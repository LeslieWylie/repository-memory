#!/usr/bin/env python3
"""Discover repositories, user configuration, and adapter executables."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from models import SourceSpec

ROOT_ENV = "REPOSITORY_MEMORY_ROOT"
AUTODISCOVER_ENV = "REPOSITORY_MEMORY_AUTODISCOVER"
SOURCE_ID_ENV = "REPOSITORY_MEMORY_SOURCE_ID"
CONFIG_ENV = "REPOSITORY_MEMORY_CONFIG"
ADAPTER_ENVS = ("REPOSITORY_MEMORY_ADAPTER", "REPOSITORY_MEMORY_LEGACY_MEMORY_CLI", "LEGACY_MEMORY_CLI")
ADAPTER_CONFIG_KEYS = ("adapter", "command", "cli", "team_memory_cli", "path")
KNOWLEDGE_SUFFIXES = {".md", ".mdx", ".txt", ".rst", ".yaml", ".yml", ".json"}


def config_path() -> Path:
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "repository-memory" / "config.json"


def data_root() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "repository-memory"


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "repository-memory"


def read_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Skill config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"Skill config must be an object: {path}")
    return value


def git(root: Path, *args: str, check: bool = False) -> str | None:
    try:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        if check:
            raise
        return None


def redact_remote(value: str | None) -> str | None:
    if not value:
        return value
    if "://" not in value:
        return value.split("@", 1)[-1] if "@" in value else value
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def is_git_repo(path: Path) -> bool:
    return bool(git(path, "rev-parse", "--show-toplevel"))


def is_knowledge_dir(path: Path) -> bool:
    """Recognize a non-Git document root without treating every directory as one."""

    if not path.is_dir():
        return False
    if any((path / name).is_file() for name in ("README.md", "README.txt", "README.rst")):
        return True
    try:
        return any(item.is_file() and item.suffix.lower() in KNOWLEDGE_SUFFIXES for item in path.iterdir())
    except OSError:
        return False


def is_configured_source(path: Path) -> bool:
    """Validate an explicit/configured source without broad auto-discovery."""

    if not path.is_dir():
        return False
    if is_git_repo(path) or is_knowledge_dir(path):
        return True
    # Explicit sources may keep documents below a domain-specific directory
    # (for example ``docs/``) and need not have a top-level README.  Do not use
    # this recursive check for cwd/parent auto-discovery, where it would turn
    # an arbitrary large directory into a knowledge root.
    return content_revision(path) is not None


def content_revision(root: Path) -> str | None:
    """Return a deterministic revision for a document directory without Git."""

    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    found = False
    excluded = {".git", ".remember", ".cache", "output", "tmp", "node_modules"}
    try:
        files = sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in KNOWLEDGE_SUFFIXES
            and not any(part.startswith(".") or part in excluded for part in path.relative_to(root).parts)
        )
        for path in files:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            if len(data) > 8 * 1024 * 1024 or b"\x00" in data:
                continue
            found = True
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
    except (OSError, UnicodeError, ValueError):
        return None
    return f"content-{digest.hexdigest()[:24]}" if found else None


def detect_root(start: Path | None = None) -> Path | None:
    configured = os.environ.get(ROOT_ENV) or read_config().get("root")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(str(configured)).expanduser())
    autodiscover = os.environ.get(AUTODISCOVER_ENV, "1").lower() not in {"0", "false", "no", "off"}
    if autodiscover:
        current = (start or Path.cwd()).expanduser().resolve()
        candidates.extend([current, *current.parents])
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_dir() and (is_git_repo(candidate) or is_knowledge_dir(candidate)):
            return candidate
    return None


def resolve_root(explicit: str | None = None) -> Path:
    root = Path(explicit).expanduser() if explicit else detect_root()
    if root is None:
        raise RuntimeError("repository root not found; set REPOSITORY_MEMORY_ROOT or pass --root")
    root = root.resolve()
    if not is_configured_source(root):
        raise RuntimeError(f"knowledge root is not a readable document directory: {root}")
    return root


def repository_state(root: Path) -> dict[str, Any]:
    remote = redact_remote(git(root, "remote", "get-url", "origin"))
    branch = git(root, "branch", "--show-current")
    commit = git(root, "rev-parse", "HEAD") or content_revision(root)
    upstream_branch = remote_branch(root)
    return {
        "root": str(root),
        "repository": root.name,
        "branch": branch,
        "commit": commit,
        "dirty": bool(git(root, "status", "--porcelain")) if is_git_repo(root) else False,
        "remote": remote,
        "remote_branch": upstream_branch,
        "remote_commit": git(root, "rev-parse", f"refs/remotes/origin/{upstream_branch}") if upstream_branch else None,
    }


def remote_branch(root: Path) -> str | None:
    head = git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head and head.startswith("refs/remotes/origin/"):
        return head.rsplit("/", 1)[-1]
    branch = git(root, "branch", "--show-current")
    if branch and git(root, "show-ref", "--verify", f"refs/remotes/origin/{branch}"):
        return branch
    for candidate in ("master", "main"):
        if git(root, "show-ref", "--verify", f"refs/remotes/origin/{candidate}"):
            return candidate
    return branch


def _backend_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("backend")
    if isinstance(value, dict):
        return value
    return {key: config[key] for key in ("adapter", "command", "cli", "protocol", "config") if key in config}


def _source_from_item(item: dict[str, Any], fallback_root: Path) -> SourceSpec:
    path = Path(str(item.get("root") or item.get("path") or fallback_root)).expanduser().resolve()
    source_id = str(item.get("id") or item.get("name") or path.name)
    return SourceSpec(
        id=source_id,
        root=path,
        repository=str(item.get("repository") or source_id),
        adapter=str(item.get("adapter") or "") or None,
        remote=str(item.get("remote") or "") or None,
        branch=str(item.get("branch") or "") or None,
        profile=str(item.get("profile") or "") or None,
        local_only=bool(item.get("local_only", item.get("localOnly", False))),
    )


def discover_sources(explicit_root: str | None = None, source_id: str | None = None) -> list[SourceSpec]:
    config = read_config()
    effective_root = explicit_root or os.environ.get(ROOT_ENV)
    configured = config.get("sources")
    fallback: Path | None = resolve_root(effective_root) if effective_root else None
    if fallback is None and not isinstance(configured, list):
        fallback = resolve_root(None)
    sources: list[SourceSpec] = []
    if isinstance(configured, list):
        for item in configured:
            if isinstance(item, dict):
                spec = _source_from_item(item, fallback or Path.cwd())
                matches_explicit = not effective_root or spec.root == fallback
                if matches_explicit and is_configured_source(spec.root):
                    sources.append(spec)
        if effective_root and sources:
            sources = [source for source in sources if source.root == fallback]
    if not sources:
        # Evaluation snapshots have a cache-specific directory name, but
        # qrels must continue to use the canonical source id.
        if fallback is None:
            raise RuntimeError("no knowledge source configured; run init --path <directory> or source add --path <directory>")
        sources.append(_source_from_item({"id": os.environ.get(SOURCE_ID_ENV) or fallback.name, "root": str(fallback)}, fallback))
    # Multiple sources are supported, but an unspecified query must not search
    # all of them and silently mix unrelated corpora.  A configured default is
    # the stable routing boundary for CLI, MCP, and host plugins; explicit
    # ``--source``/``--root`` still wins for multi-repository workflows.
    preferred = source_id
    if preferred is None and explicit_root is None and not os.environ.get(ROOT_ENV):
        preferred = str(config.get("default_source") or os.environ.get(SOURCE_ID_ENV) or "") or None
    if preferred:
        sources = [source for source in sources if source.id == preferred]
        if not sources:
            raise RuntimeError(f"source not found: {preferred}")
    return sources


def _write_config(config: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def add_source(
    path: str,
    source_id: str | None = None,
    repository: str | None = None,
    profile: str | None = None,
    local_only: bool = False,
) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not is_configured_source(root):
        raise RuntimeError(f"source is not a readable knowledge directory: {root}")
    identifier = str(source_id or root.name)
    config = read_config()
    configured = config.get("sources") if isinstance(config.get("sources"), list) else []
    item = {"id": identifier, "root": str(root), "repository": str(repository or identifier)}
    if profile:
        item["profile"] = profile
    if local_only:
        item["local_only"] = True
    replaced = False
    updated = []
    for existing in configured:
        if not isinstance(existing, dict):
            continue
        if str(existing.get("id") or "") == identifier or Path(str(existing.get("root") or "")).expanduser().resolve() == root:
            updated.append({**existing, **item})
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(item)
    config["sources"] = updated
    config_path_value = _write_config(config)
    return {
        "id": identifier,
        "root": str(root),
        "repository": item["repository"],
        "profile": profile,
        "local_only": bool(item.get("local_only", False)),
        "config": str(config_path_value),
        "canonical_repo_changed": False,
    }


def remove_source(source_id: str) -> dict[str, Any]:
    config = read_config()
    configured = config.get("sources") if isinstance(config.get("sources"), list) else []
    remaining = [item for item in configured if not isinstance(item, dict) or str(item.get("id") or "") != source_id]
    if len(remaining) == len(configured):
        raise RuntimeError(f"source not found: {source_id}")
    config["sources"] = remaining
    config_path_value = _write_config(config)
    return {"removed": source_id, "config": str(config_path_value), "canonical_repo_changed": False}


def configured_sources() -> list[dict[str, Any]]:
    configured = read_config().get("sources")
    if not isinstance(configured, list):
        return []
    result = []
    for item in configured:
        if not isinstance(item, dict):
            continue
        root = Path(str(item.get("root") or item.get("path") or "")).expanduser()
        result.append({**item, "root": str(root), "exists": root.is_dir(), "discoverable": is_configured_source(root)})
    return result


def adapter_config(spec: SourceSpec) -> dict[str, Any]:
    config = read_config()
    values = _backend_config(config)
    source_overrides = config.get("source_adapters")
    if isinstance(source_overrides, dict) and isinstance(source_overrides.get(spec.id), dict):
        values = {**values, **source_overrides[spec.id]}
    return values


def configured_adapter(spec: SourceSpec) -> Path | None:
    values = adapter_config(spec)
    explicitly_configured: list[Path] = []
    if spec.adapter:
        explicitly_configured.append(Path(spec.adapter).expanduser())
    for key in ADAPTER_CONFIG_KEYS:
        if values.get(key):
            explicitly_configured.append(Path(str(values[key])).expanduser())

    # CI and shared hosts use this switch to prevent accidental discovery of
    # a developer's private adapter.  An adapter explicitly attached to the
    # selected source remains valid: tests and users that deliberately opt in
    # must not be silently converted to the local fallback.
    disabled = os.environ.get("REPOSITORY_MEMORY_DISABLE_ADAPTER", "").lower() in {"1", "true", "yes"}
    candidates: list[Path] = list(explicitly_configured)
    if disabled:
        return _first_executable(candidates)
    for env_name in ADAPTER_ENVS:
        if os.environ.get(env_name):
            candidates.append(Path(os.environ[env_name]).expanduser())

    # Do not let an unrelated checkout next to the current directory become a
    # backend for an arbitrary document root.  Explicit config/env adapters
    # above remain valid.  The old legacy-memory executable is intentionally not
    # auto-discovered; it is an internal compatibility adapter and must be
    # opted into through user configuration.
    git_source = is_git_repo(spec.root)
    bases = [spec.root, *spec.root.parents, Path.cwd().resolve(), *Path.cwd().resolve().parents] if git_source else [spec.root]
    for base in bases:
        candidates.extend([
            base / "tools" / "repository-memory-adapter",
            base / "bin" / "repository-memory-adapter",
        ])
    path_adapter_names = ["repository-memory-adapter"]
    for name in path_adapter_names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    return _first_executable(candidates)


def _first_executable(candidates: list[Path]) -> Path | None:
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if str(resolved) in seen:
            continue
        seen.add(str(resolved))
        # Windows does not expose Unix execute bits for a configured Python
        # adapter.  A deliberate .py adapter is still runnable through the
        # current interpreter; .mjs remains runnable through Node.
        runnable_script = resolved.suffix.lower() in {".mjs", ".py"}
        if resolved.is_file() and (runnable_script or os.access(resolved, os.X_OK)):
            return resolved
    return None


def adapter_protocol(path: Path, spec: SourceSpec) -> str:
    configured = adapter_config(spec).get("protocol")
    if configured:
        return str(configured)
    if path.suffix == ".mjs" or path.name == "legacy-memory":
        return "legacy-legacy-memory"
    return "json-adapter"


def fingerprint(spec: SourceSpec) -> str:
    remote = spec.remote or git(spec.root, "remote", "get-url", "origin") or ""
    value = f"{spec.id}\0{spec.root}\0{remote}".encode()
    return hashlib.sha256(value).hexdigest()[:24]


def config_summary() -> dict[str, Any]:
    config = read_config()
    return {
        "path": str(config_path()),
        "exists": config_path().exists(),
        "data_root": str(data_root()),
        "cache_root": str(cache_root()),
        "default_source": str(config.get("default_source") or os.environ.get(SOURCE_ID_ENV) or "") or None,
    }
