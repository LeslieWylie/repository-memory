#!/usr/bin/env python3
"""Small, provider-neutral client for an optional local MemoryCore gateway.

The Skill owns this client; a vendor MemoryCore checkout remains an external
runtime dependency. No credential is returned from this module or written to the
repository.  The client deliberately uses the v3 isolation fields so a local
MemoryCore instance does not silently become a shared global memory bucket.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit


class MemoryCoreError(RuntimeError):
    """A safe, user-actionable MemoryCore error."""


def _config_path() -> Path:
    explicit = os.environ.get("REPOSITORY_MEMORY_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "repository-memory" / "config.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _legacy_team_config() -> dict[str, Any]:
    path = os.environ.get("LEGACY_MEMORY_CONFIG")
    if not path:
        path = str(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "legacy-memory" / "config.json")
    return _read_json(Path(path).expanduser())


def _secret_from_keychain(service: str, account: str) -> str | None:
    if os.name != "posix" or not shutil_which("security"):
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def _secret_from_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def shutil_which(command: str) -> str | None:
    """Local copy to keep this module dependency-free and easy to test."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _safe_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    if path == "/v3":
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _redacted_endpoint(value: str | None) -> str | None:
    if not value:
        return value
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class MemoryCoreConfig:
    endpoint: str | None
    api_key: str | None
    team_id: str | None
    agent_id: str | None
    user_id: str | None
    timeout: float = 8.0
    use_keychain: bool = True
    credential_source: str | None = None

    @classmethod
    def discover(cls) -> MemoryCoreConfig:
        config = _read_json(_config_path())
        memory = config.get("memorycore")
        if not isinstance(memory, dict):
            memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}
        backend = config.get("backend") if isinstance(config.get("backend"), dict) else {}
        legacy = _legacy_team_config()

        endpoint = _safe_endpoint(_first_string(
            os.environ.get("REPOSITORY_MEMORY_MEMORY_URL"),
            os.environ.get("TDAI_MEMORY_ENDPOINT"),
            memory.get("endpoint"),
            memory.get("baseUrl"),
            memory.get("memoryBaseUrl"),
            backend.get("memoryEndpoint"),
            backend.get("memoryBaseUrl"),
            legacy.get("memoryBaseUrl"),
            legacy.get("memory_endpoint"),
        ))

        api_key = _first_string(
            os.environ.get("REPOSITORY_MEMORY_API_KEY"),
            os.environ.get("TDAI_MEMORY_API_KEY"),
            os.environ.get("TDAI_GATEWAY_API_KEY"),
            memory.get("apiKey"),
            memory.get("memoryApiKey"),
            memory.get("gatewayApiKey"),
            _secret_from_file(os.environ.get("REPOSITORY_MEMORY_GATEWAY_API_KEY_FILE") or memory.get("gateway_api_key_file")),
        )
        credential_source = "environment/config" if api_key else None
        use_keychain = memory.get("useKeychain", True) is not False
        if not api_key and use_keychain:
            account = _first_string(memory.get("keychainAccount"), os.environ.get("USER"), getpass.getuser()) or "memory"
            api_key = _secret_from_keychain(str(memory.get("keychainService") or "repository-memorycore"), account)
            if api_key:
                credential_source = "keychain"

        team_id = _first_string(
            os.environ.get("REPOSITORY_MEMORY_TEAM_ID"),
            memory.get("team_id"), memory.get("teamId"),
            legacy.get("teamId"), legacy.get("team_id"),
            "local",
        )
        agent_id = _first_string(
            os.environ.get("REPOSITORY_MEMORY_AGENT_ID"),
            memory.get("agent_id"), memory.get("agentId"),
            legacy.get("agentId"), legacy.get("agent_id"),
            os.environ.get("OPENCLAW_AGENT_ID"),
            "repository-memory",
        )
        user_id = _first_string(
            os.environ.get("REPOSITORY_MEMORY_USER_ID"),
            memory.get("user_id"), memory.get("userId"),
            legacy.get("userId"), legacy.get("user_id"),
            getpass.getuser(),
        )
        try:
            timeout = max(0.5, min(60.0, float(os.environ.get("REPOSITORY_MEMORY_TIMEOUT", memory.get("timeout", 8.0)))))
        except (TypeError, ValueError):
            timeout = 8.0
        return cls(endpoint, api_key, team_id, agent_id, user_id, timeout, use_keychain, credential_source)

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.team_id and self.agent_id and self.user_id)

    @property
    def identity(self) -> dict[str, str]:
        return {"team_id": self.team_id or "", "agent_id": self.agent_id or "", "user_id": self.user_id or ""}


class MemoryCoreClient:
    def __init__(self, config: MemoryCoreConfig | None = None):
        self.config = config or MemoryCoreConfig.discover()
        self._health: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return self.config.configured

    def _url(self, path: str) -> str:
        if not self.config.endpoint:
            raise MemoryCoreError("MemoryCore endpoint is not configured")
        return f"{self.config.endpoint.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "content-type": "application/json"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        # The reference v3 router requires an instance/service header in
        # addition to the team/agent/user isolation fields in the JSON body.
        # In local standalone mode the team bucket is the stable instance key.
        headers["x-tdai-service-id"] = self.config.team_id or "local"
        return headers

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.config.configured:
            raise MemoryCoreError("MemoryCore is not fully configured: endpoint and identity are required")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = request.Request(self._url(path), data=data, headers=self._headers(), method=method)
        try:
            parsed = urlsplit(self.config.endpoint or "")
            # Local MemoryCore must not be routed through a user's global HTTP
            # proxy (common on VPN/dev machines); remote configured endpoints
            # continue to use the normal urllib proxy behavior.
            opener = request.build_opener(request.ProxyHandler({})) if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else request
            with opener.open(req, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
        except (OSError, error.URLError, ValueError) as exc:
            safe = _redacted_endpoint(self.config.endpoint) or "MemoryCore"
            raise MemoryCoreError(f"MemoryCore request failed at {safe}: {exc}") from exc
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise MemoryCoreError("MemoryCore returned non-JSON data") from exc
        if not isinstance(value, dict):
            raise MemoryCoreError("MemoryCore returned an invalid response")
        code = value.get("code")
        if isinstance(code, int) and code != 0:
            message = str(value.get("message") or "MemoryCore business error")
            raise MemoryCoreError(f"MemoryCore error {code}: {message[:240]}")
        return value

    @staticmethod
    def _data(value: dict[str, Any]) -> dict[str, Any]:
        data = value.get("data")
        return data if isinstance(data, dict) else value

    def health(self, refresh: bool = False, probe_layers: bool = False) -> dict[str, Any]:
        if self._health is not None and not refresh and (not probe_layers or "layers" in self._health):
            return dict(self._health)
        configured = _read_json(_config_path())
        memory_config = configured.get("memorycore") if isinstance(configured.get("memorycore"), dict) else {}
        model_ref = _first_string(memory_config.get("llm_model"))
        model_name = model_ref.rsplit("/", 1)[-1] if model_ref else None
        result: dict[str, Any] = {
            "supported_layers": ["L0", "L1", "L2", "L3"],
            "configured": self.config.configured,
            "reachable": False,
            "status": "not_configured" if not self.config.configured else "unreachable",
            "endpoint": _redacted_endpoint(self.config.endpoint),
            "identity": {"team_id": bool(self.config.team_id), "agent_id": bool(self.config.agent_id), "user_id": bool(self.config.user_id)},
            "credential_source": self.config.credential_source,
            "embedding": {"available": False, "strategy": "keyword-only"},
            "llm": {
                "configured": bool(model_name),
                "provider": "openai-compatible" if model_name else None,
                "model": model_name,
                "base_url": _redacted_endpoint(_first_string(memory_config.get("llm_base_url"))),
            },
        }
        if not self.config.configured:
            result["layers"] = {layer: {"status": "not_configured", "reachable": False} for layer in result["supported_layers"]}
            self._health = result
            return dict(result)
        try:
            response = self._request("GET", "/health")
            data = self._data(response)
            result.update({"reachable": True, "status": "ready", "server": {k: data.get(k) for k in ("status", "version", "uptime", "stores") if k in data}})
            if probe_layers:
                probes = {
                    "L0": ("/v3/conversation/query", {"limit": 1}),
                    "L1": ("/v3/atomic/query", {"limit": 1}),
                    "L2": ("/v3/scenario/ls", {"path_prefix": ""}),
                    "L3": ("/v3/core/read", {}),
                }
                layer_status: dict[str, Any] = {}
                for layer, (endpoint, body) in probes.items():
                    try:
                        probe_response = self._data(self._request("POST", endpoint, self._scoped(body)))
                        if layer in {"L0", "L1"}:
                            values = probe_response.get("messages" if layer == "L0" else "items", [])
                            values = values if isinstance(values, list) else []
                            count = len(values)
                            data_status = "present" if count else "empty"
                            layer_status[layer] = {
                                "status": "ready",
                                "reachable": True,
                                "endpoint": endpoint,
                                "data_status": data_status,
                                "record_count_sampled": count,
                                "readback_verified": True,
                            }
                        elif layer == "L2":
                            entries = probe_response.get("entries") or probe_response.get("items") or []
                            entries = entries if isinstance(entries, list) else []
                            accepted = sum(1 for item in entries if isinstance(item, dict) and item.get("accepted") is True)
                            pending = max(0, len(entries) - accepted)
                            layer_status[layer] = {
                                "status": "ready",
                                "reachable": True,
                                "endpoint": endpoint,
                                "data_status": "empty" if not entries else "accepted" if accepted else "candidate",
                                "record_count": len(entries),
                                "accepted_count": accepted,
                                "pending_count": pending,
                                "readback_verified": True,
                            }
                        else:
                            content = str(probe_response.get("content") or "").strip()
                            metadata = probe_response.get("metadata") if isinstance(probe_response.get("metadata"), dict) else {}
                            accepted = probe_response.get("accepted") is True or metadata.get("status") == "accepted" or metadata.get("accepted") is True
                            layer_status[layer] = {
                                "status": "ready",
                                "reachable": True,
                                "endpoint": endpoint,
                                "data_status": "accepted" if accepted else "present" if content else "empty",
                                "record_count": 1 if content else 0,
                                "accepted": bool(accepted),
                                "readback_verified": True,
                            }
                    except MemoryCoreError as exc:
                        layer_status[layer] = {"status": "unavailable", "reachable": False, "endpoint": endpoint, "error": str(exc)}
                result["layers"] = layer_status
        except MemoryCoreError as exc:
            result.update({"error": str(exc)})
            if probe_layers:
                result["layers"] = {layer: {"status": "unreachable", "reachable": False} for layer in result["supported_layers"]}
        self._health = result
        return dict(result)

    def _scoped(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return {**self.config.identity, **(body or {})}

    def _search_layer(self, layer: str, query: str, limit: int) -> list[dict[str, Any]]:
        endpoint = {
            "L0": "/v3/conversation/search",
            "L1": "/v3/atomic/search",
        }[layer]
        data = self._data(self._request("POST", endpoint, self._scoped({"query": query, "limit": limit})))
        values = data.get("messages" if layer == "L0" else "items", [])
        if not isinstance(values, list):
            return []
        results = []
        for item in values:
            if not isinstance(item, dict):
                continue
            record_id = str(item.get("id") or "")
            content = str(item.get("content") or "")
            if not record_id or not content:
                continue
            background = item.get("background")
            if isinstance(background, str):
                try:
                    background = json.loads(background)
                except json.JSONDecodeError:
                    background = None
            linked_evidence = item.get("linked_evidence")
            if linked_evidence is None and isinstance(background, dict) and any(background.get(key) for key in ("repository", "commit", "path", "locator")):
                linked_evidence = [background]
            results.append({
                "id": f"memorycore:{layer}:{record_id}",
                "kind": "conversation" if layer == "L0" else "atomic",
                "title": item.get("type") or layer,
                "content": content,
                "excerpt": content,
                "memory_layer": layer,
                "memory_type": item.get("type") or ("conversation" if layer == "L0" else "atomic"),
                "score": item.get("score", 0),
                "updated_at": item.get("updated_at") or item.get("timestamp"),
                "linked_evidence": linked_evidence or [],
                "_native_memory": True,
                "citation": {
                    "source": "memorycore",
                    "memory_id": record_id,
                    "layer": layer,
                    "evidence": content,
                    "locator": {"layer": layer, "memory_id": record_id, "session_id": item.get("session_id")},
                    "valid": True,
                    "generated": False,
                    "accepted": True,
                    "linked_evidence": linked_evidence or [],
                },
            })
        return results

    def _add_conversation(self, session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist L0 in the live store and let the native pipeline extract L1.

        ``/seed`` is a batch/offline runner in the reference gateway: it writes
        a disposable seed workspace and destroys its pipeline after completion.
        It is useful for export experiments, but it does not populate the
        service's live v3 search surface.  The v3 conversation mutation is the
        durable service path and triggers the same L0 -> L1 pipeline.
        """

        response = self._request(
            "POST",
            "/v3/conversation/add",
            self._scoped({"session_id": session_id, "messages": messages}),
        )
        return self._data(response)

    def observe_l1(self, session_id: str, limit: int = 100, not_before: str | None = None) -> dict[str, Any]:
        """Observe the asynchronous atomic extraction for one L0 session.

        The service does not promise that conversation/add and atomic extraction
        complete in the same request.  Callers must use this observation to
        distinguish ``pending`` from an actually searchable L1 record.
        """

        try:
            records = self._query_layer_records("L1", session_id, limit)
        except MemoryCoreError as exc:
            return {"status": "unknown", "count": 0, "error": str(exc)}
        if not_before:
            recent: list[dict[str, Any]] = []
            for item in records:
                created = str(item.get("created_at") or item.get("updated_at") or "")
                if created and created >= not_before:
                    recent.append(item)
            records = recent
        return {
            "status": "verified" if records else "pending",
            "count": len(records),
            "record_ids": [str(item.get("id")) for item in records if item.get("id")],
        }

    def write_scenario(self, path: str, content: str, summary: str | None = None) -> dict[str, Any]:
        """Write an L2 scenario candidate; acceptance is deliberately separate."""

        return self._data(self._request("POST", "/v3/scenario/write", self._scoped({
            "path": path,
            "content": content,
            "summary": summary,
        })))

    def read_scenario(self, path: str) -> dict[str, Any]:
        return self._data(self._request("POST", "/v3/scenario/read", self._scoped({"path": path})))

    def delete_scenario(self, path: str) -> dict[str, Any]:
        """Remove an explicitly targeted scenario, primarily for test cleanup."""

        return self._data(self._request("POST", "/v3/scenario/rm", self._scoped({"path": path})))

    def write_core(self, content: str) -> dict[str, Any]:
        """Write L3 only from an explicit promotion workflow."""

        return self._data(self._request("POST", "/v3/core/write", self._scoped({"content": content})))

    def read_core(self) -> dict[str, Any]:
        return self._data(self._request("POST", "/v3/core/read", self._scoped({})))

    def _query_layer_records(self, layer: str, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        endpoint = "/v3/conversation/query" if layer == "L0" else "/v3/atomic/query"
        data = self._data(self._request("POST", endpoint, self._scoped({"session_id": session_id, "limit": limit})))
        values = data.get("messages" if layer == "L0" else "items", [])
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    def _verify_l0_write(self, session_id: str, accepted_ids: list[str]) -> dict[str, Any]:
        """Verify durable L0 persistence without claiming async L1 is complete."""

        try:
            records = self._query_layer_records("L0", session_id, max(100, len(accepted_ids)))
        except MemoryCoreError as exc:
            return {"l0_verified": False, "l1_status": "unknown", "verification_error": str(exc)}
        found = {str(item.get("id")) for item in records if item.get("id")}
        l0_verified = bool(accepted_ids) and set(accepted_ids).issubset(found)
        # Conversation/add triggers L1 asynchronously and may intentionally
        # wait for a threshold/timer.  Do not report a false synchronous L1
        # success; later search/doctor calls can observe the extracted record.
        return {"l0_verified": l0_verified, "l1_status": "pending" if l0_verified else "unknown"}

    def _search_profiles(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Read the structured L2/L3 surfaces and rank only lexical matches."""
        terms = [term.casefold() for term in re.findall(r"[\w一-龥.-]{2,}", query, re.UNICODE)]
        results: list[dict[str, Any]] = []
        try:
            listed = self._data(self._request("POST", "/v3/scenario/ls", self._scoped({}))).get("entries", [])
            if isinstance(listed, list):
                for entry in listed[:100]:
                    if not isinstance(entry, dict) or str(entry.get("path", "")).endswith("/"):
                        continue
                    path = str(entry.get("path") or "")
                    file_data = self._data(self._request("POST", "/v3/scenario/read", self._scoped({"path": path})))
                    content = str(file_data.get("content") or "")
                    score = sum(content.casefold().count(term) for term in terms)
                    if score <= 0:
                        continue
                    metadata = file_data.get("metadata") if isinstance(file_data.get("metadata"), dict) else {}
                    generated = bool(entry.get("generated") or file_data.get("generated") or metadata.get("generated"))
                    accepted = bool(entry.get("accepted") is True or file_data.get("accepted") is True or metadata.get("accepted") is True)
                    evidence_status = "generated" if generated else "primary" if accepted else "pending"
                    results.append({
                        "id": f"memorycore:L2:{path}", "kind": "scenario", "title": path,
                        "content": content, "excerpt": content[:800], "path": f"scenario/{path}",
                        "memory_layer": "L2", "memory_type": "scenario", "score": score,
                        "evidence_status": evidence_status, "generated": generated, "accepted": accepted,
                        "_native_memory": True,
                        "citation": {"source": "memorycore", "memory_id": path, "layer": "L2", "path": f"scenario/{path}", "evidence": content, "locator": {"path": path}, "valid": True, "generated": generated, "accepted": accepted},
                    })
        except MemoryCoreError:
            pass
        try:
            profile = self._data(self._request("POST", "/v3/core/read", self._scoped({})))
            content = str(profile.get("content") or "")
            score = sum(content.casefold().count(term) for term in terms)
            if content and score > 0:
                metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
                generated = bool(profile.get("generated") or metadata.get("generated"))
                accepted = bool(profile.get("accepted") is True or metadata.get("accepted") is True)
                evidence_status = "generated" if generated else "primary" if accepted else "pending"
                results.append({
                    "id": "memorycore:L3:profile", "kind": "profile", "title": "profile",
                    "content": content, "excerpt": content[:800], "path": "core/profile",
                    "memory_layer": "L3", "memory_type": "profile", "score": score,
                    "evidence_status": evidence_status, "generated": generated, "accepted": accepted,
                    "_native_memory": True,
                    "citation": {"source": "memorycore", "memory_id": "profile", "layer": "L3", "path": "core/profile", "evidence": content, "locator": {"path": "core/profile"}, "valid": True, "generated": generated, "accepted": accepted},
                })
        except MemoryCoreError:
            pass
        results.sort(key=lambda item: (-float(item.get("score", 0)), str(item.get("id"))))
        return results[:limit]

    def _search_local_candidates(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Expose unaccepted post-turn candidates as candidates, never facts."""

        data_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "repository-memory"
        import hashlib

        identity_text = json.dumps(self.config.identity, sort_keys=True)
        namespace = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:20]
        directory = data_root / "autocapture" / "identities" / namespace / "candidates"
        terms = [term.casefold() for term in re.findall(r"[\w一-龥.-]{2,}", query, re.UNICODE)]
        results: list[dict[str, Any]] = []
        if not directory.is_dir():
            return results
        for path in sorted(directory.rglob("*.md"), reverse=True):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            score = sum(content.casefold().count(term) for term in terms)
            if score <= 0:
                continue
            relative = "autocapture/" + path.relative_to(data_root / "autocapture" / "identities" / namespace).as_posix()
            results.append({
                "id": f"autocapture:L2:{relative}",
                "kind": "scenario",
                "title": relative,
                "content": content,
                "excerpt": content[:800],
                "path": relative,
                "memory_layer": "L2",
                "memory_type": "candidate",
                "score": score,
                "evidence_status": "pending",
                "generated": True,
                "accepted": False,
                "_native_memory": True,
                "citation": {
                    "source": "local-memory",
                    "memory_id": relative,
                    "layer": "L2",
                    "path": relative,
                    "evidence": content,
                    "locator": {"path": relative},
                    "valid": True,
                    "generated": True,
                    "accepted": False,
                },
            })
        return results[:limit]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.config.configured:
            return []
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self._search_layer, layer, query, limit): layer for layer in ("L0", "L1")}
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except MemoryCoreError:
                    continue
        results.extend(self._search_profiles(query, limit))
        results.extend(self._search_local_candidates(query, limit))
        results.sort(key=lambda item: (-float(item.get("score", 0)), str(item.get("id"))))
        return results[:limit]

    def get(self, result_id: str) -> dict[str, Any]:
        if result_id.startswith("autocapture:L2:"):
            data_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "repository-memory"
            import hashlib

            namespace = hashlib.sha256(json.dumps(self.config.identity, sort_keys=True).encode("utf-8")).hexdigest()[:20]
            path = data_root / "autocapture" / "identities" / namespace / Path(result_id.split(":", 2)[-1]).relative_to("autocapture")
            if path.is_file() and path.is_relative_to(data_root) and ".." not in path.relative_to(data_root).parts:
                content = path.read_text(encoding="utf-8")
                relative = "autocapture/" + path.relative_to(data_root / "autocapture" / "identities" / namespace).as_posix()
                return {"id": result_id, "layer": "L2", "memory": {"content": content, "path": relative, "status": "candidate"}, "citation": {"source": "local-memory", "memory_id": relative, "layer": "L2", "path": relative, "evidence": content, "valid": True, "generated": True, "accepted": False}}
            raise MemoryCoreError(f"local candidate not found: {result_id}")
        match = re.match(r"^memorycore:(L[01]):(.+)$", result_id)
        if match:
            layer, record_id = match.groups()
            endpoint = "/v3/conversation/query" if layer == "L0" else "/v3/atomic/query"
            data = self._data(self._request("POST", endpoint, self._scoped({"limit": 100})))
            values = data.get("messages" if layer == "L0" else "items", [])
            for item in values if isinstance(values, list) else []:
                if isinstance(item, dict) and str(item.get("id")) == record_id:
                    return {"id": result_id, "layer": layer, "memory": item, "citation": {"source": "memorycore", "memory_id": record_id, "layer": layer, "valid": True}}
            raise MemoryCoreError(f"MemoryCore record not found: {result_id}")
        if result_id == "memorycore:L3:profile":
            return {"id": result_id, "layer": "L3", "memory": self._data(self._request("POST", "/v3/core/read", self._scoped({}))), "citation": {"source": "memorycore", "memory_id": "profile", "layer": "L3", "valid": True}}
        if result_id.startswith("memorycore:L2:"):
            path = result_id.split(":", 2)[-1]
            return {"id": result_id, "layer": "L2", "memory": self._data(self._request("POST", "/v3/scenario/read", self._scoped({"path": path}))), "citation": {"source": "memorycore", "memory_id": path, "layer": "L2", "path": path, "valid": True}}
        raise MemoryCoreError(f"not a MemoryCore id: {result_id}")

    def delete_conversation(self, message_ids: list[str]) -> dict[str, Any]:
        if not message_ids:
            return {"deleted_count": 0}
        return self._data(self._request("POST", "/v3/conversation/delete", self._scoped({"message_ids": message_ids})))

    @staticmethod
    def _seed_payload(input_path: Path) -> dict[str, Any]:
        text = input_path.read_text(encoding="utf-8")
        try:
            value: Any = json.loads(text)
        except json.JSONDecodeError:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            value = rows
        if isinstance(value, dict) and "sessions" in value:
            return value
        if isinstance(value, dict) and "messages" in value:
            return {"sessions": [{"sessionKey": str(value.get("session_id") or value.get("sessionKey") or "repository-memory-session"), "conversations": [value["messages"]]}]}
        if isinstance(value, list) and value and all(isinstance(row, dict) and "sessionKey" in row for row in value):
            return {"sessions": value}
        rows = value if isinstance(value, list) else [value]
        messages = [row for row in rows if isinstance(row, dict) and row.get("role") and row.get("content")]
        if not messages:
            raise MemoryCoreError("session input requires sessions, messages, or JSONL role/content rows")
        return {"sessions": [{"sessionKey": "repository-memory-session", "conversations": [messages]}]}

    def ingest(self, input_path: Path) -> dict[str, Any]:
        payload = self._seed_payload(input_path)
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        if not sessions:
            raise MemoryCoreError("session input contains no sessions")
        results: list[dict[str, Any]] = []
        for index, session in enumerate(sessions):
            if not isinstance(session, dict):
                continue
            session_id = str(session.get("sessionKey") or session.get("session_id") or f"repository-memory-session-{index}")
            raw_messages = session.get("messages")
            if not isinstance(raw_messages, list):
                raw_messages = session.get("conversations")
            if isinstance(raw_messages, list) and raw_messages and isinstance(raw_messages[0], list):
                flattened = [message for round_messages in raw_messages if isinstance(round_messages, list) for message in round_messages]
            else:
                flattened = raw_messages if isinstance(raw_messages, list) else []
            messages = []
            for message in flattened:
                if not isinstance(message, dict) or not message.get("role") or not message.get("content"):
                    continue
                item = {"role": str(message["role"]), "content": str(message["content"])}
                if message.get("timestamp"):
                    item["timestamp"] = str(message["timestamp"])
                messages.append(item)
            if not messages:
                continue
            result = self._add_conversation(session_id, messages)
            accepted_ids = result.get("accepted_ids") if isinstance(result.get("accepted_ids"), list) else []
            verification = self._verify_l0_write(session_id, [str(item) for item in accepted_ids])
            results.append({"session_id": session_id, "messages": len(messages), **result, **verification})
        if not results:
            raise MemoryCoreError("session input contains no valid role/content messages")
        l0_verified = all(bool(item.get("l0_verified")) for item in results)
        return {
            "pipeline": "MemoryCore v3 conversation/add L0->async L1",
            "result": {"sessions": results, "l0_recorded": sum(int(item.get("total_count") or 0) for item in results)},
            "memory": self.health(refresh=True),
            "verified": l0_verified,
            "l0_verified": l0_verified,
            "l1_status": "pending" if l0_verified else "unknown",
            "canonical_repo_changed": False,
        }


def native_memory_client() -> MemoryCoreClient:
    return MemoryCoreClient()
