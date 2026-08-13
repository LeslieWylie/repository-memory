#!/usr/bin/env python3
"""Optional Memmy adapter for local semantic memory.

Memmy is treated as a provider, not as a second canonical repository.  The
adapter only returns native memory records and keeps the provider/layer in the
citation so the repository runtime can keep Git evidence separate.
"""

from __future__ import annotations

import getpass
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit


class MemmyError(RuntimeError):
    """A safe, actionable Memmy error."""


def _config_path() -> Path:
    explicit = os.environ.get("REPOSITORY_MEMORY_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "repository-memory" / "config.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_error(value: Any) -> str | None:
    if not value:
        return None
    # Memmy already redacts some gateway tokens, but the adapter must not
    # assume that every installed version does so.
    return re.sub(r"\[[^\]]+\]", "[REDACTED]", str(value)[:300])


@dataclass(frozen=True)
class MemmyConfig:
    endpoint: str | None
    enabled: bool
    source: str
    profile_id: str
    user_id: str
    # Local embedding model cold-start can take several seconds.  A short
    # health probe is useful, but using the same 3s deadline for search makes
    # a healthy Memmy instance look unavailable on its first semantic query.
    timeout: float = 15.0


def _configured() -> dict[str, Any]:
    value = _read_json(_config_path()).get("memmy")
    return value if isinstance(value, dict) else {}


def memmy_config() -> MemmyConfig:
    value = _configured()
    endpoint = _safe_endpoint(
        os.environ.get("REPOSITORY_MEMORY_MEMMY_ENDPOINT")
        or value.get("endpoint")
    )
    enabled_value = os.environ.get("REPOSITORY_MEMORY_MEMMY_ENABLED")
    enabled = (
        enabled_value.lower() in {"1", "true", "yes", "on"}
        if enabled_value is not None
        else bool(value.get("enabled") is True)
    )
    return MemmyConfig(
        endpoint=endpoint,
        enabled=enabled,
        source=_first_string(os.environ.get("REPOSITORY_MEMORY_MEMMY_SOURCE"), value.get("source"), "repository-memory") or "repository-memory",
        profile_id=_first_string(os.environ.get("REPOSITORY_MEMORY_MEMMY_PROFILE"), value.get("profile_id"), "repository-memory") or "repository-memory",
        user_id=_first_string(os.environ.get("REPOSITORY_MEMORY_USER_ID"), value.get("user_id"), getpass.getuser()) or getpass.getuser(),
        timeout=float(value.get("timeout", 15.0) or 15.0),
    )


class MemmyClient:
    def __init__(self, config: MemmyConfig | dict[str, Any] | None = None):
        if config is None:
            self.config = memmy_config()
        elif isinstance(config, MemmyConfig):
            self.config = config
        else:
            value = config.get("memmy") if isinstance(config.get("memmy"), dict) else config
            value = value if isinstance(value, dict) else {}
            self.config = MemmyConfig(
                endpoint=_safe_endpoint(value.get("endpoint")),
                enabled=value.get("enabled") is True,
                source=_first_string(value.get("source"), "repository-memory") or "repository-memory",
                profile_id=_first_string(value.get("profile_id"), "repository-memory") or "repository-memory",
                user_id=_first_string(value.get("user_id"), getpass.getuser()) or getpass.getuser(),
                timeout=float(value.get("timeout", 15.0) or 15.0),
            )

    @property
    def configured(self) -> bool:
        return bool(self.config.enabled and self.config.endpoint)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise MemmyError("Memmy is not configured; run repository-memory memmy configure")
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"accept": "application/json"}
        if body is not None:
            headers["content-type"] = "application/json"
        url = f"{self.config.endpoint.rstrip('/')}{path}"
        req = request.Request(url, data=payload, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise MemmyError(f"Memmy {method} {path} returned HTTP {exc.code}: {detail}") from exc
        except (OSError, error.URLError, TimeoutError) as exc:
            raise MemmyError(f"Memmy request failed at {url}: {exc}") from exc
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise MemmyError("Memmy returned non-JSON data") from exc
        return value if isinstance(value, dict) else {"value": value}

    def health(self) -> dict[str, Any]:
        base = {
            "backend": "memmy",
            "configured": self.configured,
            "endpoint": self.config.endpoint,
            "source": self.config.source,
            "profile_id": self.config.profile_id,
            "supported_layers": ["L1", "L2", "L3", "Skill"],
            "semantic": {"available": False, "strategy": "keyword-only"},
        }
        if not self.configured:
            return {**base, "reachable": False, "status": "not_configured"}
        try:
            payload = self._request("GET", "/api/v1/health")
        except MemmyError as exc:
            return {**base, "reachable": False, "status": "unreachable", "error": str(exc)[:300]}
        models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
        embedding = models.get("embedding") if isinstance(models.get("embedding"), dict) else {}
        embedding_available = bool(embedding.get("configured") is True and embedding.get("lastError") is None)
        strategy = "local-hybrid" if embedding_available else "keyword-only"
        model_report = {
            name: {
                "provider": value.get("provider"),
                "model": value.get("model"),
                "configured": value.get("configured") is True,
                "remote": value.get("remote"),
                "last_ok_at": value.get("lastOkAt"),
                "last_error": _safe_error(value.get("lastError")),
            }
            for name, value in models.items()
            if isinstance(value, dict)
        }
        return {
            **base,
            "reachable": True,
            "status": "ready",
            "health": {
                "version": payload.get("version"),
                "storage": payload.get("storage"),
                "capabilities": payload.get("capabilities"),
                "models": model_report,
            },
            "models": model_report,
            "embedding": {
                "available": embedding_available,
                "strategy": strategy,
                "provider": embedding.get("provider"),
                "model": embedding.get("model"),
                "remote": embedding.get("remote"),
                "last_ok_at": embedding.get("lastOkAt"),
                "last_error": _safe_error(embedding.get("lastError")),
            },
        }

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        payload = self._request("POST", "/api/v1/memory/search", {
            "query": query,
            "limit": max(1, min(int(limit), 50)),
            "layers": ["L1", "L2", "L3", "Skill"],
            "includeInjectedContext": False,
            "verbose": True,
            "source": self.config.source,
            "namespace": {
                "source": self.config.source,
                "profileId": self.config.profile_id,
                "userId": self.config.user_id,
            },
        })
        debug = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}
        hits = debug.get("hits") if isinstance(debug.get("hits"), list) else payload.get("hits")
        if not isinstance(hits, list):
            return []
        results: list[dict[str, Any]] = []
        for hit in hits:
            if not isinstance(hit, dict) or not hit.get("id"):
                continue
            memory_id = str(hit["id"])
            layer = str(hit.get("memoryLayer") or hit.get("layer") or "L1")
            snippet = str(hit.get("snippet") or hit.get("summary") or hit.get("title") or "").strip()
            status = str(hit.get("status") or "activated")
            results.append({
                "id": f"memmy:{layer}:{memory_id}",
                "kind": hit.get("kind") or "memory",
                "title": hit.get("title") or memory_id,
                "content": snippet,
                "excerpt": snippet,
                "memory_layer": layer,
                "memory_type": hit.get("kind") or "memory",
                "score": hit.get("score", 0),
                "updated_at": hit.get("updatedAt"),
                "_native_memory": True,
                "_memory_backend": "memmy",
                "citation": {
                    "source": "memmy",
                    "memory_id": memory_id,
                    # Memmy's Skill lane is a first-class memory layer; do
                    # not relabel it as L1 just to fit TencentDB's vocabulary.
                    "layer": layer if layer in {"L1", "L2", "L3", "Skill"} else "L1",
                    "evidence": snippet,
                    "locator": {"memory_id": memory_id},
                    "valid": bool(snippet),
                    "accepted": status in {"activated", "active"},
                    "generated": False,
                    "provenance": {
                        "source": "memmy",
                        "memory_id": memory_id,
                        "status": status,
                    },
                },
            })
        return results

    def get(self, result_id: str) -> dict[str, Any]:
        memory_id = result_id.split(":", 2)[-1] if result_id.startswith("memmy:") else result_id
        payload = self._request("GET", f"/api/v1/memory/{memory_id}")
        value = payload.get("memory") if isinstance(payload.get("memory"), dict) else payload.get("data")
        value = value if isinstance(value, dict) else payload
        if not isinstance(value, dict):
            return {"value": value}
        layer = str(value.get("memoryLayer") or value.get("layer") or "L1")
        status = str(value.get("status") or "activated")
        content = str(
            value.get("content")
            or value.get("body")
            or value.get("text")
            or value.get("summary")
            or value.get("snippet")
            or value.get("title")
            or ""
        ).strip()
        citation_layer = layer if layer in {"L1", "L2", "L3", "Skill"} else "L1"
        return {
            **value,
            "content": content,
            "memory_layer": layer,
            "memory_type": value.get("kind") or "memory",
            "_native_memory": True,
            "_memory_backend": "memmy",
            "citation": {
                "source": "memmy",
                "memory_id": memory_id,
                "layer": citation_layer,
                "evidence": content,
                "locator": {"memory_id": memory_id},
                "valid": bool(content),
                "accepted": status in {"activated", "active"},
                "generated": False,
                "provenance": {
                    "source": "memmy",
                    "memory_id": memory_id,
                    "status": status,
                },
            },
        }


def memmy_memory_client() -> MemmyClient:
    return MemmyClient()


def configure_memmy(endpoint: str, profile_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    """Enable the explicit Memmy semantic provider in user config."""

    safe = _safe_endpoint(endpoint)
    if not safe:
        raise MemmyError("Memmy endpoint must be an absolute HTTP(S) URL")
    path = _config_path()
    current = _read_json(path)
    current["memmy"] = {
        **(_configured()),
        "enabled": True,
        "endpoint": safe,
        "source": "repository-memory",
        "profile_id": profile_id or _configured().get("profile_id") or "repository-memory",
        "user_id": user_id or _configured().get("user_id") or getpass.getuser(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"ok": True, "config": str(path), "memmy": {k: v for k, v in current["memmy"].items() if "key" not in k.lower()}}
