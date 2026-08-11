#!/usr/bin/env python3
"""Adapter seam for external memory/index backends."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from discovery import adapter_config, adapter_protocol, configured_adapter
from local_memory import local_memory_store
from memorycore import MemoryCoreError, native_memory_client

from models import SourceView

SECRET_NAME_RE = __import__("re").compile(r"(^|/)(\.env(?:\.|$)|.*\.(?:pem|key|p12|pfx|secret|secrets?))$", __import__("re").I)
SECRET_CONTENT_RE = __import__("re").compile(r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{16,}|\bsk-[A-Za-z0-9_-]{16,}", __import__("re").I)
DOC_EXTENSIONS = {".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json"}
EXCLUDED_DIRS = {".git", ".remember", "output", "tmp", "node_modules"}
OPERATIONAL_DIRS = {"skills", "scripts", "tests", "test", "eval", "evals", "fixtures", "logs"}
LOCAL_REPOSITORY_ADAPTER = "repository-local-structured"


class AdapterError(RuntimeError):
    pass


def _safe_tracked_paths(root: Path, deep: bool = False) -> list[str]:
    try:
        output = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    selected = []
    for name in output.split("\0"):
        if not name or SECRET_NAME_RE.search(name.replace("\\", "/")):
            continue
        parts = Path(name).parts
        if any(part.startswith(".") for part in parts):
            continue
        excluded = EXCLUDED_DIRS | (OPERATIONAL_DIRS if not deep else set())
        if any(part in excluded for part in parts):
            continue
        path = root / name
        if path.suffix.lower() not in DOC_EXTENSIONS or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(content.encode()) > 512 * 1024 or "\x00" in content or SECRET_CONTENT_RE.search(content):
            continue
        selected.append(name)
    return sorted(selected)


@contextmanager
def safe_git_index(root: Path, deep: bool = False):
    """Restrict backend git enumeration through a temporary alternate index."""
    fd, index = tempfile.mkstemp(prefix="repository-memory-index-")
    os.close(fd)
    Path(index).unlink(missing_ok=True)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index
    try:
        subprocess.run(["git", "-C", str(root), "read-tree", "HEAD"], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        selected = set(_safe_tracked_paths(root, deep))
        tracked = set((subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], text=True, stderr=subprocess.DEVNULL)).split("\0"))
        removed = sorted(item for item in tracked - selected if item)
        if removed:
            subprocess.run(["git", "-C", str(root), "update-index", "--force-remove", "-z", "--stdin"], input="\0".join(removed), text=True, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        yield env
    finally:
        Path(index).unlink(missing_ok=True)
        Path(f"{index}.lock").unlink(missing_ok=True)


class Adapter:
    def __init__(self, executable: Path | None, source: SourceView):
        self.executable = executable
        self.source = source
        self.protocol = adapter_protocol(executable, source.spec) if executable else "local-fallback"
        self.name = executable.name if executable else LOCAL_REPOSITORY_ADAPTER
        self._memory_probe: dict[str, Any] | None = None
        self.native_memory = native_memory_client()
        # An adapter without an external repository executable can still own
        # the native MemoryCore plane.  Expose that fact in ingest/doctor
        # receipts instead of leaking the repository fallback name.
        if self.native_memory.configured:
            self.name = "native-memorycore"
        self.local_memory = local_memory_store()

    @property
    def available(self) -> bool:
        return self.executable is not None

    def _settings(self) -> dict[str, Any]:
        """Merge Skill settings with the legacy adapter's user config.

        The Skill remains provider-neutral.  This compatibility seam is the
        only place that knows the legacy adapter may keep its own config file;
        secrets are used for requests but never returned in diagnostics.
        """
        values = dict(adapter_config(self.source.spec))
        if self.protocol != "legacy-legacy-memory":
            return values
        config_file = values.get("config") or os.environ.get("LEGACY_MEMORY_CONFIG")
        if not config_file:
            config_file = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "legacy-memory" / "config.json"
        try:
            external = json.loads(Path(str(config_file)).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            external = {}
        if isinstance(external, dict):
            values = {**external, **values}
        return values

    def memory_endpoint(self) -> str | None:
        if self.protocol != "legacy-legacy-memory":
            return None
        settings = self._settings()
        return (
            os.environ.get("REPOSITORY_MEMORY_MEMORY_URL")
            or os.environ.get("LEGACY_MEMORY_MEMORY_URL")
            or os.environ.get("TDAI_MEMORY_ENDPOINT")
            or str(settings.get("memoryBaseUrl") or settings.get("memory_endpoint") or "").strip()
            or None
        )

    def memory_status(self) -> dict[str, Any]:
        """Report support and live reachability for the optional memory plane."""
        if self.native_memory.configured:
            return self.native_memory.health()
        if self._memory_probe is not None:
            return dict(self._memory_probe)
        supported = ["L0", "L1", "L2", "L3"] if self.protocol == "legacy-legacy-memory" else []
        endpoint = self.memory_endpoint()
        result: dict[str, Any] = {
            "supported_layers": supported,
            "configured": bool(endpoint),
            "reachable": False if not endpoint else None,
            "status": "not_configured" if not endpoint else "unknown",
        }
        if not endpoint:
            if self.protocol == "local-fallback":
                return self.local_memory.health()
            self._memory_probe = result
            return result
        parsed = urlsplit(endpoint)
        safe_endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        result["endpoint"] = safe_endpoint
        health_path = f"{parsed.path.rstrip('/')}/health"
        health_url = urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))
        headers: dict[str, str] = {"accept": "application/json"}
        api_key = self._settings().get("memoryApiKey") or os.environ.get("TDAI_MEMORY_API_KEY")
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        try:
            request = urllib.request.Request(health_url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=1) as response:
                result.update({"reachable": 200 <= response.status < 400, "status": "ready" if response.status < 400 else "error"})
        except (OSError, urllib.error.URLError, ValueError) as exc:
            result.update({"reachable": False, "status": "unreachable", "error": str(exc).replace(str(endpoint), safe_endpoint)[:240]})
        self._memory_probe = result
        if result.get("reachable") is True:
            return result
        if self.protocol == "local-fallback":
            fallback = self.local_memory.health()
            fallback["native"] = result
            fallback["fallback"] = True
            return fallback
        return result

    def memory_ready(self) -> bool:
        return self.memory_status().get("reachable") is True

    def memory_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        if not self.native_memory.configured:
            return self.local_memory.search(query, limit) if self.protocol == "local-fallback" else []
        try:
            return self.native_memory.search(query, limit)
        except MemoryCoreError as exc:
            try:
                fallback = self.local_memory.search(query, limit)
            except (OSError, RuntimeError, ValueError):
                fallback = []
            if fallback:
                return fallback
            raise AdapterError(str(exc)) from exc

    def memory_get(self, result_id: str) -> dict[str, Any]:
        if result_id.startswith("local:") or (not self.native_memory.configured and self.protocol == "local-fallback"):
            try:
                return self.local_memory.get(result_id)
            except (KeyError, OSError, RuntimeError, ValueError) as exc:
                raise AdapterError(str(exc)) from exc
        if not self.native_memory.configured:
            raise AdapterError("MemoryCore is not configured")
        try:
            return self.native_memory.get(result_id)
        except MemoryCoreError as exc:
            raise AdapterError(str(exc)) from exc

    def _invoke(self, args: list[str], timeout: int = 300, deep: bool = False) -> dict[str, Any]:
        if not self.executable:
            raise AdapterError("adapter executable unavailable")
        values = self._settings()
        suffix = self.executable.suffix.lower()
        if suffix == ".mjs":
            command = ["node", str(self.executable)]
        elif suffix == ".py":
            command = [sys.executable, str(self.executable)]
        else:
            command = [str(self.executable)]
        env = os.environ.copy()
        env.update({
            "REPOSITORY_MEMORY_SOURCE_ROOT": str(self.source.path),
            "REPOSITORY_MEMORY_SOURCE_ID": self.source.spec.id,
            "REPOSITORY_MEMORY_SOURCE_COMMIT": self.source.commit or "",
        })
        if self.protocol == "legacy-legacy-memory" and self.memory_endpoint():
            env["LEGACY_MEMORY_MEMORY_URL"] = self.memory_endpoint() or ""
        temporary_config: Path | None = None
        if self.protocol == "legacy-legacy-memory" and self.memory_endpoint() and args and args[0] in {"search", "ingest-session"} and "--config" not in args:
            configured_file = values.get("config") or os.environ.get("LEGACY_MEMORY_CONFIG")
            if not configured_file:
                configured_file = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "legacy-memory" / "config.json"
            configured_path = Path(str(configured_file)).expanduser()
            if configured_path.exists():
                try:
                    payload = json.loads(configured_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = {}
                if isinstance(payload, dict) and not payload.get("memoryBaseUrl"):
                    fd, name = tempfile.mkstemp(prefix="repository-memory-team-config-", suffix=".json")
                    os.close(fd)
                    temporary_config = Path(name)
                    temporary_config.write_text(json.dumps({**payload, "memoryBaseUrl": self.memory_endpoint()}, ensure_ascii=False), encoding="utf-8")
                    args = [*args, "--config", str(temporary_config)]
        if temporary_config is None and values.get("config") and "--config" not in args:
            args = [*args, "--config", str(Path(str(values["config"])).expanduser())]
        try:
            with safe_git_index(self.source.path, deep=deep) as safe_env:
                safe_env.update(env)
                result = subprocess.run(command + args, cwd=self.source.path, env=safe_env, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterError(str(exc)) from exc
        finally:
            if temporary_config is not None:
                temporary_config.unlink(missing_ok=True)
        if result.returncode:
            raise AdapterError((result.stderr or result.stdout or f"adapter exited {result.returncode}").strip())
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"adapter returned non-JSON output: {result.stdout[:500]}") from exc
        return value if isinstance(value, dict) else {"value": value}

    def doctor(self) -> dict[str, Any]:
        if self.protocol == "legacy-legacy-memory":
            try:
                return self._invoke(["repo", "status", self.source.spec.id])
            except AdapterError as first:
                raise AdapterError(str(first)) from first
        return self._invoke(["doctor", "--json"])

    def add(self) -> dict[str, Any]:
        if self.protocol == "legacy-legacy-memory":
            return self._invoke(["repo", "add", "--path", str(self.source.path), "--name", self.source.spec.id])
        return self._invoke(["sync", "--json", "--source", self.source.spec.id])

    def sync(self, deep: bool = False) -> dict[str, Any]:
        if self.protocol == "legacy-legacy-memory":
            return self._invoke(["repo", "sync", self.source.spec.id], deep=deep)
        return self._invoke(["sync", "--json", "--source", self.source.spec.id], deep=deep)

    def search(self, query: str, limit: int, deep: bool = False) -> dict[str, Any]:
        if self.protocol == "legacy-legacy-memory":
            args = ["search", query, "--repo", self.source.spec.id, "--limit", str(limit)]
            return self._invoke(args, deep=deep)
        args = ["search", "--query", query, "--source", self.source.spec.id, "--limit", str(limit), "--json"]
        if deep:
            args.append("--deep")
        return self._invoke(args, deep=deep)

    def get(self, result_id: str) -> dict[str, Any]:
        if self.protocol == "legacy-legacy-memory":
            return self._invoke(["get", result_id])
        return self._invoke(["get", "--id", result_id, "--json"])

    def ingest_session(self, input_path: Path) -> dict[str, Any]:
        """Explicitly submit a generic session payload to the adapter."""
        if self.native_memory.configured:
            try:
                return self.native_memory.ingest(input_path)
            except MemoryCoreError as exc:
                fallback = self.local_memory.ingest(input_path)
                fallback["native_error"] = str(exc)
                fallback["fallback"] = True
                return fallback
        if self.protocol == "local-fallback":
            try:
                return self.local_memory.ingest(input_path)
            except (OSError, ValueError, RuntimeError) as exc:
                raise AdapterError(str(exc)) from exc
        if self.protocol == "legacy-legacy-memory":
            return self._invoke(["ingest-session", "--input", str(input_path)])
        return self._invoke(["ingest-session", "--input", str(input_path), "--json"])


def discover_adapter(source: SourceView) -> Adapter:
    return Adapter(configured_adapter(source.spec), source)


def adapter_status(adapter: Adapter, probe_memory_layers: bool = False) -> dict[str, Any]:
    native_health = adapter.native_memory.health(refresh=probe_memory_layers, probe_layers=probe_memory_layers) if adapter.native_memory.configured else adapter.memory_status()
    native_ready = adapter.native_memory.configured and native_health.get("status") == "ready"
    local_ready = native_health.get("backend") == "local-memory" and native_health.get("status") == "ready"
    if native_ready or local_ready:
        capabilities = ["memory-doctor", "memory-search", "memory-get", "ingest-session", "local-search"]
    elif adapter.available and adapter.protocol == "legacy-legacy-memory":
        capabilities = ["doctor", "sync", "search", "get", "memory-search", "ingest-session"]
    elif adapter.available:
        capabilities = ["doctor", "sync", "search", "get"]
    elif adapter.native_memory.configured:
        capabilities = ["memory-doctor", "memory-search", "memory-get", "ingest-session"]
    else:
        capabilities = ["local-search"]
    value: dict[str, Any] = {
        "name": "native-memorycore" if native_ready else "local-memory" if local_ready else adapter.name,
        "protocol": "memorycore" if native_ready else "local-memory" if local_ready else adapter.protocol,
        "path": None if native_ready or local_ready else (str(adapter.executable) if adapter.executable else None),
        "available": adapter.available or adapter.native_memory.configured or local_ready,
        "capabilities": capabilities,
    }
    value["memory"] = native_health
    if native_ready:
        value.update({
            "healthy": True,
            "repository_backend": {
                "status": "local_on_demand",
                "legacy_adapter": adapter.name if adapter.available else None,
                "required": False,
            },
            "semantic": adapter.memory_status().get("embedding", {"available": False, "strategy": "keyword-only"}),
        })
        return value
    if local_ready:
        value.update({
            "healthy": True,
            "repository_backend": {"status": "local_on_demand", "legacy_adapter": adapter.name if adapter.available else None, "required": False},
            "semantic": native_health.get("embedding", {"available": False, "strategy": "keyword-only"}),
        })
        return value
    if adapter.available:
        pass
    elif adapter.native_memory.configured:
        value["healthy"] = adapter.memory_status().get("status") == "ready"
        if not value["healthy"]:
            value["error"] = adapter.memory_status().get("error") or "MemoryCore is not reachable"
        value["semantic"] = adapter.memory_status().get("embedding", {"available": False, "strategy": "keyword-only"})
        return value
    else:
        value["healthy"] = True
        value["error"] = "repository adapter executable not found; using the local structured repository index"
        value["backend"] = LOCAL_REPOSITORY_ADAPTER
        return value
    try:
        report = adapter.doctor()
        value.update({"healthy": True, "report": report})
        capabilities = report.get("capabilities") if isinstance(report, dict) else None
        if isinstance(capabilities, list):
            value["capabilities"] = sorted(set(value["capabilities"]) | set(capabilities))
        value["semantic"] = report.get("semantic", {"available": None}) if isinstance(report, dict) else {"available": None}
    except AdapterError as exc:
        if adapter.native_memory.configured and adapter.memory_status().get("status") == "ready":
            value.update({
                "healthy": True,
                "repository_backend": {"status": "optional_unavailable", "error": str(exc)[:240]},
                "semantic": adapter.memory_status().get("embedding", {"available": False, "strategy": "keyword-only"}),
            })
        else:
            value.update({"healthy": False, "error": str(exc)})
    return value
