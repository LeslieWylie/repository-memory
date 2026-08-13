#!/usr/bin/env python3
"""Small client seam for TencentDB MemoryKnowledge.

MemoryCore owns conversation memory and asset metadata.  MemoryKnowledge owns
Wiki/code-graph content.  Keeping this client separate makes that boundary
visible in doctor and allows an installation to opt into the upstream service
without replacing the citation-first repository index.

The client is intentionally conservative: knowledge results are never marked
verified unless the repository-memory caller can validate their path and
commit against the selected source snapshot.
"""

from __future__ import annotations

import getpass
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit, urlunsplit


class KnowledgeError(RuntimeError):
    """A safe, actionable MemoryKnowledge error."""


_DOC_EXTENSIONS = {".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json"}
_EXCLUDED_DIRS = {".git", ".remember", "output", "tmp", "node_modules"}
_SECRET_NAME = re.compile(r"(^|/)(\.env(?:\.|$)|.*\.(?:pem|key|p12|pfx|secret|secrets?))$", re.I)
_SECRET_CONTENT = re.compile(
    r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{16,}|\bsk-[A-Za-z0-9_-]{16,}",
    re.I,
)


def _config_path() -> Path:
    explicit = os.environ.get("REPOSITORY_MEMORY_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "repository-memory" / "config.json"


def _read_config() -> dict[str, Any]:
    try:
        value = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _base_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    if path == "/v3":
        path = ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _data(value: dict[str, Any]) -> dict[str, Any]:
    payload = value.get("data")
    return payload if isinstance(payload, dict) else value


def _knowledge_config() -> dict[str, Any]:
    config = _read_config()
    value = config.get("knowledge")
    return value if isinstance(value, dict) else {}


class KnowledgeClient:
    def __init__(self, config: dict[str, Any] | None = None):
        config = _read_config() if config is None else config
        memory = config.get("memorycore") if isinstance(config.get("memorycore"), dict) else {}
        knowledge = config.get("knowledge") if isinstance(config.get("knowledge"), dict) else {}
        self.endpoint = _base_url(_first(
            os.environ.get("REPOSITORY_MEMORY_KNOWLEDGE_URL"),
            os.environ.get("KNOWLEDGE_SERVICE_URL"),
            knowledge.get("endpoint"),
            knowledge.get("base_url"),
        ))
        self.service_id = _first(os.environ.get("REPOSITORY_MEMORY_KNOWLEDGE_SERVICE_ID"), knowledge.get("service_id"), memory.get("team_id"), "local")
        self.team_id = _first(os.environ.get("REPOSITORY_MEMORY_KNOWLEDGE_TEAM_ID"), knowledge.get("team_id"), memory.get("team_id"), "local")
        self.user_id = _first(os.environ.get("REPOSITORY_MEMORY_USER_ID"), knowledge.get("user_id"), memory.get("user_id"), getpass.getuser())
        self.agent_id = _first(os.environ.get("REPOSITORY_MEMORY_AGENT_ID"), knowledge.get("agent_id"), memory.get("agent_id"), "repository-memory")
        self.wiki_id = _first(os.environ.get("REPOSITORY_MEMORY_KNOWLEDGE_WIKI_ID"), knowledge.get("wiki_id"))
        self.code_graph_id = _first(os.environ.get("REPOSITORY_MEMORY_KNOWLEDGE_CODE_GRAPH_ID"), knowledge.get("code_graph_id"))
        self.timeout = 3.0

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.service_id and self.team_id)

    @property
    def identity(self) -> dict[str, str]:
        return {
            "service_id": self.service_id or "",
            "team_id": self.team_id or "",
            "user_id": self.user_id or "",
            "agent_id": self.agent_id or "",
        }

    def _url(self, path: str) -> str:
        if not self.endpoint:
            raise KnowledgeError("MemoryKnowledge endpoint is not configured")
        return f"{self.endpoint.rstrip('/')}/v3/{path.lstrip('/')}"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise KnowledgeError("MemoryKnowledge is not configured: endpoint and tenant identity are required")
        payload = {**self.identity, **(body or {})}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-tdai-service-id": self.service_id or "local",
        }
        req = request.Request(self._url(path), data=data, headers=headers, method=method)
        try:
            parsed = urlsplit(self.endpoint or "")
            opener = request.build_opener(request.ProxyHandler({})) if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else request
            # Raw reindex is a synchronous SQLite rebuild for a large Wiki.
            # Keep health/search probes short, but allow the explicit sync
            # operation to complete instead of returning a false failure while
            # the service is still rebuilding its derived index.
            timeout = 30.0 if path == "wiki/raw/reindex" else self.timeout
            with opener.open(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except (OSError, error.URLError, ValueError) as exc:
            raise KnowledgeError(f"MemoryKnowledge request failed at {_safe_url(self.endpoint)}: {exc}") from exc
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise KnowledgeError("MemoryKnowledge returned non-JSON data") from exc
        if not isinstance(value, dict):
            raise KnowledgeError("MemoryKnowledge returned an invalid response")
        code = value.get("code")
        if isinstance(code, int) and code != 0:
            raise KnowledgeError(f"MemoryKnowledge error {code}: {str(value.get('message') or 'request failed')[:240]}")
        return value

    def health(self) -> dict[str, Any]:
        base = {
            "backend": "tencentdb-memoryknowledge",
            "configured": self.configured,
            "endpoint": _safe_url(self.endpoint),
            "components": ["wiki", "code-graph"],
            "semantic": {"available": False, "strategy": "keyword-only-unless-service-reports-otherwise"},
        }
        if not self.configured:
            return {**base, "reachable": False, "status": "not_configured"}
        try:
            req = request.Request(f"{self.endpoint.rstrip('/')}/health", headers={"accept": "application/json"}, method="GET")
            parsed = urlsplit(self.endpoint or "")
            opener = request.build_opener(request.ProxyHandler({})) if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else request
            with opener.open(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
            return {**base, "reachable": True, "status": "ready", "health": payload if isinstance(payload, dict) else {}}
        except (OSError, error.URLError, ValueError, json.JSONDecodeError) as exc:
            return {**base, "reachable": False, "status": "unreachable", "error": str(exc)[:240]}

    def list_wikis(self) -> dict[str, Any]:
        return _data(self._request("POST", "wiki/list", {}))

    def create_wiki(self, name: str) -> dict[str, Any]:
        return _data(self._request("POST", "wiki/create", {"name": name}))

    def raw_write(self, wiki_id: str, files: list[dict[str, str]]) -> dict[str, Any]:
        return _data(self._request("POST", "wiki/raw/write", {"wiki_id": wiki_id, "files": files}))

    def raw_reindex(self, wiki_id: str, changed: list[str], deleted: list[str]) -> dict[str, Any]:
        return _data(self._request("POST", "wiki/raw/reindex", {"wiki_id": wiki_id, "changed": changed, "deleted": deleted}))

    def raw_ls(self, wiki_id: str) -> list[dict[str, Any]]:
        value = _data(self._request("POST", "wiki/raw/ls", {"wiki_id": wiki_id}))
        items = value.get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def raw_rm(self, wiki_id: str, filenames: list[str]) -> dict[str, Any]:
        if not filenames:
            return {"items": [], "deleted": []}
        return _data(self._request("POST", "wiki/raw/rm", {"wiki_id": wiki_id, "filenames": filenames}))

    def search(self, wiki_id: str, query: str, limit: int = 5) -> dict[str, Any]:
        return _data(self._request("POST", "wiki/search", {"wiki_id": wiki_id, "query": query, "limit": limit}))

    def sync_source(self, root: Path, wiki_id: str, *, deep: bool = False) -> dict[str, Any]:
        """Upload safe tracked text files to a configured Wiki asset.

        This is a derived-index write.  It never changes the Git worktree.  A
        caller must explicitly configure a wiki id; the client will not create
        or select an arbitrary remote asset during ordinary search.
        """

        files: list[dict[str, str]] = []
        for path in _tracked_documents(root, deep=deep):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if len(content.encode("utf-8")) > 512 * 1024 or "\x00" in content or _SECRET_CONTENT.search(content):
                continue
            files.append({"filename": str(path.relative_to(root)), "content": content})
        changed = [item["filename"] for item in files]
        current_names = set(changed)
        previous_names = {
            str(item.get("filename") or item.get("path") or item.get("name"))
            for item in self.raw_ls(wiki_id)
            if isinstance(item, dict) and str(item.get("filename") or item.get("path") or item.get("name"))
        }
        deleted = sorted(previous_names - current_names)
        removed = self.raw_rm(wiki_id, deleted)
        written = []
        batch: list[dict[str, str]] = []
        batch_bytes = 0
        # MemoryKnowledge's current raw/write route accepts at most 10 files
        # per request (the service layer has a larger internal limit).  Keep
        # the adapter at the HTTP boundary limit so the behavior is stable
        # across the vendored and user-level runtimes.
        for item in files:
            item_bytes = len(item["content"].encode("utf-8"))
            if batch and (len(batch) >= 10 or batch_bytes + item_bytes > 4 * 1024 * 1024):
                written.append(self.raw_write(wiki_id, batch))
                batch = []
                batch_bytes = 0
            batch.append(item)
            batch_bytes += item_bytes
        if batch:
            written.append(self.raw_write(wiki_id, batch))
        reindexed = self.raw_reindex(wiki_id, changed, deleted)
        return {"ok": True, "wiki_id": wiki_id, "files": len(files), "batches": len(written), "deleted": deleted, "removed": removed, "reindex": reindexed, "canonical_repo_changed": False}


def _tracked_documents(root: Path, *, deep: bool = False) -> list[Path]:
    import subprocess

    try:
        raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    result: list[Path] = []
    for name in raw.split("\0"):
        if not name or _SECRET_NAME.search(name.replace("\\", "/")):
            continue
        parts = Path(name).parts
        if any(part.startswith(".") for part in parts) or any(part in _EXCLUDED_DIRS for part in parts):
            continue
        if not deep and any(part in {"skills", "scripts", "tests", "test", "eval", "evals", "fixtures", "logs"} for part in parts):
            continue
        path = root / name
        if path.suffix.lower() in _DOC_EXTENSIONS and path.is_file():
            result.append(path)
    return sorted(result)


def status() -> dict[str, Any]:
    return KnowledgeClient().health()
