#!/usr/bin/env python3
"""Generic repository-memory runtime.

This file is intentionally the small compatibility facade. Discovery, snapshots,
adapters, citation validation, fallback search, and MCP transport live behind
separate seams so a backend can change without changing the Skill contract.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from adapters import Adapter, AdapterError, adapter_status, discover_adapter
from citation import (
    evidence_status,
    lines,
    locate,
    normalize_path,
    result_is_verified,
    validate,
    validate_memory,
)
from discovery import (
    add_source,
    cache_root,
    config_summary,
    configured_sources,
    data_root,
    discover_sources,
    fingerprint,
    git,
    remove_source,
    repository_state,
    resolve_root,
)
from fallback import SECRET_CONTENT, SECRET_NAME, _claim_support, query_terms
from fallback import search as fallback_search
from local_index import ensure as ensure_local_index
from local_index import status as local_index_status
from mcp_server import serve
from memorycore import native_memory_client
from snapshot import prepare_view

from models import SourceSpec, SourceView

SCHEMA_VERSION = 3
REPOSITORY_BACKEND = "repository-local-structured"
# Keep fabricated-marker detection conservative: the ``ZZZ...`` form is used
# by synthetic negative probes and should never fall through to generic
# repository results after an agent translates the surrounding sentence.
NEGATIVE_RE = re.compile(r"不存在|虚构|假想|没有(?:这|该)|fictional|nonexistent|imaginary|made[- ]up|never indexed|no such|\bZ{3,}[A-Z0-9_-]*\b", re.IGNORECASE)
TEMPORAL_RE = re.compile(r"最新|最近|目前|本周|上周|今天|昨天|latest|recent|current|this week|20\d{2}[-/]\d{1,2}", re.IGNORECASE)
CROSS_RE = re.compile(r"关联|相关|对比|分别|cross|related|compare", re.IGNORECASE)
EXACT_RE = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b|[./][\w.-]+\.(?:md|yaml|yml)", re.IGNORECASE)


def classify(query: str) -> str:
    if NEGATIVE_RE.search(query):
        return "negative"
    if TEMPORAL_RE.search(query):
        return "temporal"
    if CROSS_RE.search(query):
        return "cross-source"
    if EXACT_RE.search(query):
        return "exact"
    return "semantic"


def _sensitive_relative_path(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    parts = Path(normalized).parts
    return bool(SECRET_NAME.search(normalized) or any(part.startswith(".") for part in parts))


def _safe_document(root: Path, relative: str) -> tuple[Path, list[str]] | None:
    """Read an explicitly requested document without exposing secret material."""

    if _sensitive_relative_path(relative):
        return None
    document = (root / relative).resolve()
    try:
        document.relative_to(root.resolve())
        lines_value = document.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if SECRET_CONTENT.search("\n".join(lines_value)):
        return None
    return document, lines_value


def _source_payload(view: SourceView) -> dict[str, Any]:
    memory_only = view.commit_type == "memorycore"
    return {"id": view.spec.id, "repository": None if memory_only else view.spec.repository, "root": None if memory_only else str(view.spec.root), "path": None if memory_only else str(view.path), "branch": view.branch, "remote": view.remote_url, "commit": view.commit, "commit_type": view.commit_type, "freshness": view.freshness}


def _memory_view() -> SourceView:
    """Create a source-neutral view for MemoryCore-only environments."""

    cwd = Path.cwd().resolve()
    return SourceView(
        spec=SourceSpec(id="memorycore", root=cwd, repository="memorycore"),
        path=cwd,
        commit=None,
        branch=None,
        commit_type="memorycore",
        dirty=False,
        remote_url=None,
        remote_commit=None,
        fetch_ok=None,
        fetch_error=None,
        snapshot=False,
    )


def _openclaw_routing() -> dict[str, Any]:
    """Inspect the local host routing without making OpenClaw mandatory.

    Repository-memory is also usable from Claude, Codex, and plain CLI hosts.
    A missing OpenClaw profile therefore reports ``not_detected`` instead of
    making a generic repository doctor fail.  Only booleans and agent ids are
    returned; credentials and host-specific paths never enter this report.
    """

    candidates: list[Path] = []
    state_dir = os.environ.get("OPENCLAW_STATE_DIR")
    if state_dir:
        candidates.append(Path(state_dir).expanduser() / "openclaw.json")
    candidates.append(Path.home() / ".openclaw" / "openclaw.json")
    config_path = next((path for path in candidates if path.is_file()), None)
    if config_path is None:
        return {
            "status": "not_detected",
            "managed": False,
            "repository_mcp": "unknown",
            "builtin_memory_search": "unknown",
            "direct_file_fallback": "host-dependent",
            "guard": "not_installed",
            "agents": {"configured": [], "covered": []},
        }
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "unreadable",
            "managed": True,
            "repository_mcp": "unknown",
            "builtin_memory_search": "unknown",
            "direct_file_fallback": "unknown",
            "guard": "unknown",
            "agents": {"configured": [], "covered": []},
        }
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    default_memory = agents.get("defaults", {}).get("memorySearch") if isinstance(agents.get("defaults"), dict) else {}
    configured_agents: list[str] = []
    enabled_agent_memory = bool(isinstance(default_memory, dict) and default_memory.get("enabled") is True)
    for entry in agents.get("list", []) if isinstance(agents.get("list"), list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        agent_id = entry["id"]
        configured_agents.append(agent_id)
        memory_search = entry.get("memorySearch")
        if isinstance(memory_search, dict) and memory_search.get("enabled") is True:
            enabled_agent_memory = True
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    active_memory = entries.get("active-memory") if isinstance(entries.get("active-memory"), dict) else {}
    memmy = entries.get("memmy-memory") if isinstance(entries.get("memmy-memory"), dict) else {}
    autocapture = entries.get("repository-memory-autocapture") if isinstance(entries.get("repository-memory-autocapture"), dict) else {}
    autocapture_config = autocapture.get("config") if isinstance(autocapture.get("config"), dict) else {}
    guard_enabled = autocapture.get("enabled") is not False and autocapture_config.get("enabled") is not False and autocapture_config.get("guardEnabled") is True
    enforcement = autocapture_config.get("enforcement") if autocapture_config.get("enforcement") in {"audit", "enforce"} else "audit"
    allowed_agents = autocapture_config.get("agentIds")
    if not isinstance(allowed_agents, list) or not all(isinstance(item, str) for item in allowed_agents):
        allowed_agents = []
    covered_agents = configured_agents if not allowed_agents else [agent_id for agent_id in configured_agents if agent_id in allowed_agents]
    excluded_agents = [] if not allowed_agents else [agent_id for agent_id in configured_agents if agent_id not in allowed_agents]
    mcp_servers = config.get("mcp", {}).get("servers", {}) if isinstance(config.get("mcp"), dict) else {}
    repository_mcp = "ready" if isinstance(mcp_servers, dict) and isinstance(mcp_servers.get("repository-memory"), dict) else "missing"
    builtin_status = "disabled" if not enabled_agent_memory else "enabled"
    active_status = "disabled" if active_memory.get("enabled") is False else "enabled"
    legacy_status = "disabled" if memmy.get("enabled") is False and plugins.get("slots", {}).get("memory") != "memmy-memory" else "legacy-active"
    allowlist_ok = bool(configured_agents) and bool(allowed_agents) and set(allowed_agents).issubset(set(configured_agents))
    coverage_ok = bool(configured_agents) and (not allowed_agents or set(covered_agents) == set(configured_agents))
    guard_ready = allowlist_ok if allowed_agents else coverage_ok
    guard_status = "enforce" if guard_enabled and guard_ready and enforcement == "enforce" else "audit" if guard_enabled and guard_ready else "partial" if guard_enabled else "disabled"
    managed_ready = repository_mcp == "ready" and builtin_status == "disabled" and active_status == "disabled" and guard_status in {"audit", "enforce"}
    return {
        "status": "ready" if managed_ready else "degraded",
        "managed": True,
        "repository_mcp": repository_mcp,
        "builtin_memory_search": builtin_status,
        "direct_file_fallback": "blocked" if guard_status == "enforce" else "audited" if guard_status == "audit" else "host-dependent",
        "guard": guard_status,
        "guard_enforcement": enforcement if guard_enabled else "disabled",
        "legacy_memory": legacy_status,
        "active_memory": active_status,
        "agents": {"configured": configured_agents, "covered": covered_agents, "excluded": excluded_agents, "scope": "allowlist" if allowed_agents else "all"},
    }


def _interleave_results(buckets: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Keep one source from starving every other configured source.

    Each adapter has already ranked its own results.  Interleaving is a
    deterministic source-level policy, not cross-backend score fusion, and it
    makes a multi-repository top-k capable of representing more than the first
    source in configuration order.
    """

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if index >= len(bucket):
                continue
            item = bucket[index]
            identifier = str(item.get("id") or "")
            if identifier and identifier in seen:
                continue
            if identifier:
                seen.add(identifier)
            output.append(item)
            if len(output) >= limit:
                return output
    return output


def _discover_views(root: Path | None, source_id: str | None, scope: str, local: bool = False) -> tuple[list[SourceView], SourceView | None, str | None]:
    """Resolve repository views without making MemoryCore depend on a repo."""

    discovery_error: str | None = None
    try:
        specs = discover_sources(str(root) if root else None, source_id)
    except RuntimeError as exc:
        specs = []
        discovery_error = str(exc)
    repository_views = [prepare_view(spec, local=local) for spec in specs] if scope in {"repository", "all"} else []
    memory_view = _memory_view() if scope in {"memory", "all"} else None
    return repository_views, memory_view, discovery_error


def _empty(query: str, mode: str, source_views: list[SourceView], reason: str, *, scope: str = "repository", backend: str | None = None) -> dict[str, Any]:
    groups = {name: {"verified": [], "candidates": [], "results": [], "abstain": True} for name in ("repository", "memory")}
    return {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "mode": mode,
        "scope": scope,
        "sources": [_source_payload(view) for view in source_views],
        "verified": [] if scope == "all" else groups[scope]["verified"],
        "candidates": [] if scope == "all" else groups[scope]["candidates"],
        "results": [] if scope == "all" else groups[scope]["results"],
        "groups": groups if scope == "all" else None,
        "abstain": True,
        "freshness": {view.spec.id: view.freshness for view in source_views},
        "diagnostics": {"scope": scope, "adapter": backend, "result_count": 0, "reason": reason},
    }


def _raw_results(payload: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    results: list[tuple[dict[str, Any], str]] = []
    long_term = payload.get("long_term")
    if isinstance(long_term, dict):
        for item in long_term.get("results", []):
            if isinstance(item, dict):
                results.append(({
                    **item,
                    "_source": item.get("_source") or long_term.get("query_source") or "long_term",
                    "_memory_query_source": long_term.get("query_source"),
                    "_memory_strategy": long_term.get("strategy"),
                }, "long_term"))
    repositories = payload.get("repositories")
    if isinstance(repositories, list):
        for repo in repositories:
            if not isinstance(repo, dict):
                continue
            for source, section_name in (("wiki", "memory"), ("code", "code")):
                section = repo.get(section_name)
                if not isinstance(section, dict):
                    continue
                for item in section.get("results", []):
                    if isinstance(item, dict):
                        item = {**item, "_repository": repo.get("repository"), "_commit": repo.get("commit"), "_source": section.get("query_source") or source}
                        results.append((item, source))
    direct = payload.get("results")
    if isinstance(direct, list):
        for item in direct:
            if isinstance(item, dict):
                results.append((item, "adapter"))
    return results


def normalize_item(item: dict[str, Any], view: SourceView, source_type: str, query: str | None = None) -> dict[str, Any]:
    citation = item.get("citation") if isinstance(item.get("citation"), dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    backend_path = citation.get("path") or item.get("path") or item.get("file_path") or item.get("source_path") or metadata.get("path") or metadata.get("source_path")
    path = normalize_path(backend_path)
    excerpt = item.get("snippet") or item.get("excerpt") or item.get("body") or item.get("content") or item.get("text") or item.get("summary") or metadata.get("excerpt") or metadata.get("text")
    start, end = lines({**metadata, **item, **citation})
    if start is None:
        start, end = locate(view.path, path, excerpt)
    commit = citation.get("commit") or item.get("commit") or item.get("_commit") or metadata.get("commit") or metadata.get("revision") or view.commit
    status = evidence_status(item, citation)
    memory_backend = citation.get("source") if citation.get("source") in {"memorycore", "local-memory"} else None
    native = bool(item.get("_native_memory") or memory_backend)
    memory_backend = memory_backend or (item.get("_memory_backend") if native else None) or ("memorycore" if native else None)
    checked = validate_memory(citation, excerpt) if native else validate(view.path, path, start, end, excerpt, commit, view.commit)
    if not native and view.dirty and view.commit_type == "local_worktree":
        checked = {**checked, "valid": False, "stale": True, "reason": "source working tree is dirty"}
    backend_id = citation.get("memory_id") or item.get("id")
    # Evaluation and cross-adapter joins need an ID that does not change when
    # a backend assigns a different record ID. A source-scoped canonical path
    # is stable for document-level qrels; retain the backend ID separately for
    # adapter-specific diagnostics.
    memory_id = backend_id if native else (f"{view.spec.id}:{path}" if path else backend_id)
    result_id = str(item.get("id") or memory_id) if native else memory_id
    citation_repository = citation.get("repository") or item.get("repository") or item.get("_repository") or metadata.get("repository") or (None if native else view.spec.repository)
    memory_layer = item.get("memory_layer") or item.get("layer") or item.get("level") or metadata.get("memory_layer") or metadata.get("layer") or metadata.get("level") or citation.get("layer")
    memory_type = item.get("memory_type") or item.get("type") or metadata.get("memory_type") or metadata.get("type") or citation.get("memory_type")
    support = item.get("support") if isinstance(item.get("support"), dict) else None
    if support is None and query is not None:
        support = _claim_support(query_terms(query), str(excerpt or ""), start or 1, end or start or 1)
    support = support or {"matched_terms": [], "unmatched_terms": [], "coverage": 1.0, "claim_support": "unknown", "supporting_spans": []}
    result = {
        "id": result_id,
        "memory_id": memory_id,
        "kind": item.get("kind") or item.get("type") or source_type,
        "title": item.get("title") or item.get("name") or item.get("heading") or path or "untitled",
        "source": memory_backend if native else view.spec.id,
        "repository": citation_repository,
        "path": path,
        "commit": commit,
        "commit_type": memory_backend if native else view.commit_type,
        "line_start": start,
        "line_end": end,
        "excerpt": excerpt,
        "support": support,
        "evidence_status": status,
        "generated": bool(citation.get("generated", item.get("generated", False))),
        "accepted": citation.get("accepted", item.get("accepted")),
        "related": item.get("related") or item.get("links") or [],
        "linked_evidence": item.get("linked_evidence") or citation.get("linked_evidence") or [],
        "memory": {
            "layer": memory_layer,
            "type": memory_type,
            "query_source": item.get("_memory_query_source") or citation.get("source") or item.get("_source"),
            "strategy": item.get("_memory_strategy"),
        } if source_type == "long_term" or native else None,
        "freshness": item.get("freshness") or ({"state": memory_backend or "memory", "updated_at": item.get("updated_at")} if native else view.freshness),
        "citation": {
            "source": memory_backend if native else citation.get("source") or item.get("_source") or source_type,
            "repository": citation_repository,
            "source_id": memory_backend if native else view.spec.id,
            "commit": commit,
            "commit_type": memory_backend if native else view.commit_type,
            "path": path,
            "backend_path": backend_path,
            "memory_id": memory_id,
            "backend_id": backend_id,
            "line_start": start,
            "line_end": end,
            "evidence": citation.get("evidence") or excerpt,
            "locator": citation.get("locator") or ({"start_line": start, "end_line": end} if start else None),
            "generated": bool(citation.get("generated", item.get("generated", False))),
            "accepted": citation.get("accepted", item.get("accepted")),
            "layer": memory_layer,
            "memory_type": memory_type,
            "linked_evidence": item.get("linked_evidence") or citation.get("linked_evidence") or [],
            "valid": checked.get("valid", False),
            "stale": checked.get("stale", False),
            "validation_reason": checked.get("reason"),
        },
    }
    result["_verified"] = (checked.get("valid") and status not in {"stale", "candidate", "pending", "inferred", "generated"}) if native else result_is_verified(checked, status, commit, view.commit)
    if not result["_verified"]:
        if checked.get("stale"):
            result["evidence_status"] = "stale"
        result["candidate_reason"] = checked.get("reason") or ("evidence status is not final" if status in {"candidate", "pending", "inferred", "generated"} else "citation incomplete")
    return result


def _split_results(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    verified, candidates = [], []
    seen: set[str] = set()
    for item in items:
        item.pop("_verified", None)
        identifier = str(item.get("id") or f"{item.get('source')}:{item.get('path')}:{item.get('line_start')}")
        if identifier in seen:
            continue
        seen.add(identifier)
        (verified if item.get("citation", {}).get("valid") and item.get("evidence_status") not in {"stale", "candidate", "pending", "inferred", "generated"} else candidates).append(item)
    return verified, candidates


def _fallback_items(view: SourceView, query: str, limit: int, deep: bool, *, stale: bool = False) -> list[dict[str, Any]]:
    try:
        view.metadata["local_index"] = ensure_local_index(view, deep)
    except (OSError, RuntimeError, TypeError, ValueError):
        # The index is disposable acceleration state; citation-first file
        # scanning remains the safe fallback if cache creation fails.
        view.metadata.pop("local_index", None)
    raw_items = fallback_search(view, query, limit, deep)
    normalized = []
    for item in raw_items:
        if stale:
            item["evidence_status"] = "stale"
        citation = item.get("citation") if isinstance(item.get("citation"), dict) else {}
        citation["source"] = "repository"
        citation["backend"] = REPOSITORY_BACKEND
        item["citation"] = citation
        normalized.append(normalize_item(item, view, REPOSITORY_BACKEND, query))
    return normalized


def _fallback_is_stale(view: SourceView, local: bool) -> bool:
    """Mark fallback results stale only when the source version is unknown.

    A healthy remote snapshot is still a verifiable Git source even when the
    optional legacy Wiki adapter is broken.  Conversely, a failed fetch that
    falls back to a dirty local worktree must not be promoted to ``verified``.
    """

    if local:
        return False
    return view.freshness.get("state") not in {"fresh"}


def _sync_if_needed(adapter: Adapter, view: SourceView, deep: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    # Native memory recall is independent of the repository index.  Repository
    # search can use the local citation fallback even when no external adapter
    # or Wiki service is present.
    if not adapter.available:
        return None, None
    try:
        status = adapter.doctor()
    except AdapterError as exc:
        return None, str(exc)
    report = status.get("report") if isinstance(status.get("report"), dict) else status
    indexed = report.get("lastSyncedCommit") or report.get("indexed_commit") or report.get("indexedCommit") if isinstance(report, dict) else None
    registered = bool(report.get("wiki") or report.get("registered") or report.get("name")) if isinstance(report, dict) else False
    if indexed and view.commit and indexed == view.commit:
        return status, None
    try:
        if not registered and adapter.protocol == "legacy-legacy-memory":
            adapter.add()
        return adapter.sync(deep=deep), None
    except AdapterError as exc:
        return None, str(exc)


def _repository_items(view: SourceView, adapter: Adapter, query: str, limit: int, deep: bool, local: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Search only canonical repository evidence; never touch MemoryCore."""

    if not adapter.available:
        items = _fallback_items(view, query, limit, deep, stale=_fallback_is_stale(view, local))
        return items, {"source": view.spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "fallback": False, "optional_external_adapter": "unavailable", "memory_skipped": True, "reason": "using the configured local structured repository index"}
    _, sync_error = _sync_if_needed(adapter, view, deep=deep)
    if sync_error:
        items = _fallback_items(view, query, limit, deep, stale=_fallback_is_stale(view, local))
        return items, {"source": view.spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "fallback": False, "optional_external_adapter": adapter.name, "memory_skipped": True, "reason": sync_error}
    try:
        payload = adapter.search(query, limit, deep)
    except AdapterError as exc:
        items = _fallback_items(view, query, limit, deep, stale=_fallback_is_stale(view, local))
        return items, {"source": view.spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "fallback": False, "optional_external_adapter": adapter.name, "memory_skipped": True, "reason": str(exc)}
    normalized = [normalize_item(item, view, source, query) for item, source in _raw_results(payload)]
    return normalized[:limit], {"source": view.spec.id, "adapter": adapter.name, "protocol": adapter.protocol, "fallback": False, "memory_skipped": True}


def _memory_items(view: SourceView, adapter: Adapter, query: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Search native L0/L1 memory and preserve layer-specific citations."""

    memory = adapter.memory_status()
    if memory.get("reachable") is not True:
        return [], {"source": view.spec.id, "adapter": memory.get("backend") or "memorycore", "memory": memory, "repository_skipped": True, "fallback": False}
    try:
        native_items = adapter.memory_search(query, limit)
    except AdapterError as exc:
        return [], {"source": view.spec.id, "adapter": "memorycore", "memory": memory, "repository_skipped": True, "fallback": True, "reason": str(exc)}
    normalized = [normalize_item(item, view, memory.get("backend") or "memorycore", query) for item in native_items]
    return normalized[:limit], {"source": view.spec.id, "adapter": memory.get("backend") or "memorycore", "memory": memory, "repository_skipped": True, "native_memory_count": len(native_items), "fallback": bool(memory.get("fallback"))}


def _package_search(query: str, mode: str, scope: str, views: list[SourceView], groups: dict[str, dict[str, Any]], diagnostics: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    selected = groups[scope] if scope != "all" else {"verified": [], "candidates": [], "results": []}
    memory_ready = any(entry.get("memory", {}).get("status") == "ready" for entry in diagnostics if isinstance(entry.get("memory"), dict))
    retrieval_mode = "grouped" if scope == "all" else "keyword-only" if scope == "memory" and memory_ready else "lexical"
    result_count = sum(len(group.get("verified", [])) for group in groups.values()) if scope == "all" else len(selected["verified"])
    candidate_count = sum(len(group.get("candidates", [])) for group in groups.values()) if scope == "all" else len(selected["candidates"])
    return {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "mode": mode,
        "scope": scope,
        "retrieval_mode": retrieval_mode,
        "sources": [_source_payload(view) for view in views],
        "verified": selected["verified"][:limit],
        "candidates": selected["candidates"][:limit],
        "results": selected["verified"][:limit],
        "groups": groups if scope == "all" else None,
        "abstain": not any(group.get("verified") for group in (groups.values() if scope == "all" else [selected])),
        "freshness": {view.spec.id: view.freshness for view in views},
        "diagnostics": {
            "scope": scope,
            "adapters": diagnostics,
            "result_count": result_count,
            "candidate_count": candidate_count,
            "retrieval_mode": retrieval_mode,
            "semantic_available": False if memory_ready else None,
            "query_terms": query_terms(query),
        },
    }


def search(root: Path | None, query: str, limit: int = 5, deep: bool = False, source_id: str | None = None, local: bool = False, scope: str = "repository") -> dict[str, Any]:
    if scope not in {"repository", "memory", "all"}:
        raise ValueError(f"unsupported scope: {scope}")
    mode = classify(query)
    repository_views, memory_view, discovery_error = _discover_views(root, source_id, scope, local)
    views = [*repository_views, *([memory_view] if memory_view else [])]
    if mode == "negative":
        return _empty(query, mode, views, "negative intent requires explicit evidence", scope=scope)
    groups = {"repository": {"verified": [], "candidates": [], "results": []}, "memory": {"verified": [], "candidates": [], "results": []}}
    diagnostics: list[dict[str, Any]] = []
    repository_verified_buckets: list[list[dict[str, Any]]] = []
    repository_candidate_buckets: list[list[dict[str, Any]]] = []
    for view in repository_views:
        adapter = discover_adapter(view)
        if scope in {"repository", "all"}:
            items, diagnostic = _repository_items(view, adapter, query, limit, deep, local)
            verified, candidates = _split_results(items)
            repository_verified_buckets.append(verified)
            repository_candidate_buckets.append(candidates)
            diagnostics.append(diagnostic)
    if memory_view is not None:
        adapter = Adapter(None, memory_view)
        if scope in {"memory", "all"}:
            items, diagnostic = _memory_items(memory_view, adapter, query, limit)
            verified, candidates = _split_results(items)
            groups["memory"]["verified"].extend(verified)
            groups["memory"]["candidates"].extend(candidates)
            diagnostics.append(diagnostic)
    if discovery_error and scope in {"repository", "all"} and not repository_views:
        diagnostics.append({"source": None, "adapter": "repository-memory", "fallback": False, "memory_skipped": True, "reason": discovery_error})
    if discovery_error and scope == "memory" and memory_view is not None:
        diagnostics.append({"source": None, "adapter": "repository-memory", "repository_skipped": True, "reason": discovery_error})
    groups["repository"]["verified"] = _interleave_results(repository_verified_buckets, limit)
    groups["repository"]["candidates"] = _interleave_results(repository_candidate_buckets, limit)
    for group in groups.values():
        group["verified"] = group["verified"][:limit]
        group["candidates"] = group["candidates"][:limit]
        group["results"] = group["verified"]
        group["abstain"] = not group["verified"]
    return _package_search(query, mode, scope, views, groups, diagnostics, limit)


def sync_index(root: Path | None, deep: bool = False, source_id: str | None = None, local: bool = False) -> dict[str, Any]:
    try:
        specs = discover_sources(str(root) if root else None, source_id)
    except RuntimeError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "sources": [],
            "repository": {"status": "not_configured", "error": str(exc)},
            "memory": Adapter(None, _memory_view()).memory_status(),
            "canonical_repo_changed": False,
            "deep": deep,
            "local": local,
        }
    results = []
    for spec in specs:
        view = prepare_view(spec, local=local)
        adapter = discover_adapter(view)
        try:
            local_index = ensure_local_index(view, deep)
            index_info = {"path": str(local_index_status(view, deep).get("path")), "indexed_commit": local_index.get("commit"), "document_count": len(local_index.get("documents", [])), "deep": deep}
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            index_info = {"available": False, "error": str(exc)}
        if adapter.protocol == "legacy-legacy-memory" and adapter.memory_status().get("status") == "ready":
            results.append({
                "source": spec.id,
                "adapter": REPOSITORY_BACKEND,
                "backend": REPOSITORY_BACKEND,
                "synced": False,
                "repository_index": "local_on_demand",
                "legacy_skipped": True,
                "optional_backend": True,
                "memory": adapter.memory_status(),
                "index": index_info,
                "freshness": view.freshness,
            })
            continue
        if not adapter.available:
            results.append({"source": spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "synced": True, "fallback_ready": True, "repository_index": "local_structured", "reason": "using the configured local structured repository index", "index": index_info, "memory": adapter.memory_status(), "freshness": view.freshness})
            continue
        try:
            status = adapter.doctor()
            report = status.get("report") if isinstance(status.get("report"), dict) else status
            registered = bool(report.get("wiki") or report.get("registered") or report.get("name")) if isinstance(report, dict) else False
            if not registered and adapter.protocol == "legacy-legacy-memory":
                adapter.add()
            synced = adapter.sync(deep=deep)
            results.append({"source": spec.id, "adapter": adapter.name, "synced": True, "repository_index": "local_structured", "index": index_info, "freshness": view.freshness, "result": synced})
        except AdapterError as exc:
            results.append({"source": spec.id, "adapter": adapter.name, "synced": False, "adapter_sync": False, "repository_index": "local_structured", "index": index_info, "fallback_ready": True, "optional_backend": True, "memory": adapter.memory_status(), "freshness": view.freshness, "error": str(exc)})
    return {"schema_version": SCHEMA_VERSION, "sources": results, "canonical_repo_changed": False, "deep": deep, "local": local}


def ingest_session(root: Path | None, input_path: str, source_id: str | None = None) -> dict[str, Any]:
    """Explicitly send a generic session JSON/JSONL payload to the adapter."""
    native = native_memory_client()
    if native.configured:
        view = _memory_view()
        adapter = Adapter(None, view)
        source_name = "memorycore"
    else:
        try:
            specs = discover_sources(str(root) if root else None, source_id)
        except RuntimeError:
            view = _memory_view()
            adapter = Adapter(None, view)
            source_name = "local-memory"
        else:
            if len(specs) != 1:
                raise RuntimeError("ingest-session requires --source when multiple sources are configured")
            view = prepare_view(specs[0], local=True)
            adapter = discover_adapter(view)
            source_name = specs[0].id
    if not adapter.available and not adapter.native_memory.configured and adapter.protocol != "local-fallback":
        raise AdapterError("session ingestion requires a configured MemoryCore or adapter")
    try:
        result = adapter.ingest_session(Path(input_path).expanduser().resolve())
    except AdapterError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "source": source_name,
            "adapter": adapter.name,
            "error": str(exc),
            "memory": adapter.memory_status(),
            "canonical_repo_changed": False,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "source": source_name,
        "adapter": adapter.name,
        "input": str(Path(input_path).expanduser().resolve()),
        "result": result,
        "memory": adapter.memory_status(),
        "write_operation": "explicit",
        "canonical_repo_changed": False,
    }


def init_source(
    path: str,
    source_id: str | None = None,
    repository: str | None = None,
    profile: str | None = None,
    sync: bool = True,
    local_only: bool = False,
) -> dict[str, Any]:
    """Register a user-owned source and optionally build its derived index."""

    registration = add_source(path, source_id, repository, profile, local_only)
    root = Path(registration["root"])
    source = registration["id"]
    synced = sync_index(root, source_id=source, local=True) if sync else None
    return {"schema_version": SCHEMA_VERSION, "initialized": True, "source": registration, "sync": synced, "canonical_repo_changed": False}


def source_list() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "sources": configured_sources(), "config": config_summary(), "canonical_repo_changed": False}


def ingest_session_payload(root: Path | None, payload: Any, source_id: str | None = None) -> dict[str, Any]:
    """Explicit MCP-friendly session ingestion without requiring a local file path."""

    if isinstance(payload, str):
        content = payload
    else:
        content = json.dumps(payload, ensure_ascii=False)
    data_dir = data_root() / "incoming"
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="session-", dir=data_dir, delete=False) as handle:
        handle.write(content)
        handle.flush()
        path = Path(handle.name)
    try:
        return ingest_session(root, str(path), source_id)
    finally:
        path.unlink(missing_ok=True)


def _capture_ledger_path() -> Path:
    path = data_root() / "autocapture" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _capture_seen(key: str) -> bool:
    path = _capture_ledger_path()
    try:
        return any(line.split("\t", 1)[0] == key for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return False


def _capture_record(key: str, result: dict[str, Any]) -> None:
    path = _capture_ledger_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(key + "\t" + json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


def capture_turn(root: Path | None, payload: Any, source_id: str | None = None) -> dict[str, Any]:
    """Capture one completed OpenClaw turn without exposing a write MCP tool.

    L0 is written and verified through the configured adapter.  L1 is observed
    asynchronously.  A durable turn creates only an L2 ``candidate``.  L3 is
    intentionally untouched and can only be changed by an explicit promotion.
    """

    from autocapture import candidate_markdown, candidate_path, candidate_store_path, normalize_turn, should_create_candidate

    if not isinstance(payload, dict):
        raise ValueError("capture-turn payload must be a JSON object")
    turn = normalize_turn(payload)
    native = native_memory_client()
    identity = native.config.identity if native.configured else {}
    # Run ids are only unique within a host/session.  Include the configured
    # memory identity so two agents cannot suppress each other's capture when
    # they share the user-level ledger.
    key_material = json.dumps(
        {"identity": identity, "session_id": turn["session_id"], "run_id": turn.get("run_id") or candidate_path(turn)},
        ensure_ascii=False,
        sort_keys=True,
    )
    key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    if _capture_seen(key):
        return {"schema_version": SCHEMA_VERSION, "ok": True, "duplicate": True, "idempotency_key": key, "canonical_repo_changed": False}

    session_payload = {"sessions": [{"sessionKey": turn["session_id"], "messages": turn["messages"]}]}
    l0_result = ingest_session_payload(root, session_payload, source_id)
    native_result = l0_result.get("result") if isinstance(l0_result.get("result"), dict) else {}
    l0_backend = native_result.get("result") if isinstance(native_result.get("result"), dict) else {}
    sessions = l0_backend.get("sessions") if isinstance(l0_backend.get("sessions"), list) else []
    session_result = sessions[0] if sessions and isinstance(sessions[0], dict) else {}
    l0 = {
        "l0_verified": bool(session_result.get("l0_verified") or native_result.get("l0_verified")),
        "record_ids": session_result.get("accepted_ids") or [],
        "status": "verified" if (session_result.get("l0_verified") or native_result.get("l0_verified")) else "unknown",
    }
    l1: dict[str, Any] = {"status": "not_configured", "count": 0}
    if native.configured and l0["l0_verified"]:
        # LLM-backed extraction is asynchronous; the local service commonly
        # needs several seconds.  This remains bounded because OpenClaw invokes
        # capture in a detached post-turn process.
        deadline = time.monotonic() + 8.0
        while True:
            l1 = native.observe_l1(turn["session_id"], not_before=turn["captured_at"])
            if l1.get("status") == "verified" or time.monotonic() >= deadline:
                break
            time.sleep(0.25)

    candidate: dict[str, Any] = {"created": False, "status": "skipped", "reason": "not durable"}
    if should_create_candidate(turn) and native.configured and l0["l0_verified"]:
        path = candidate_path(turn)
        content = candidate_markdown(turn, l0, l1)
        try:
            native.write_scenario(path, content, summary="OpenClaw completed-turn candidate")
            observed = native.read_scenario(path)
            observed_content = str(observed.get("content") or "")
            candidate = {
                "created": True,
                "status": "candidate",
                "verified": observed_content == content,
                "path": path,
                "id": f"memorycore:L2:{path}",
                "evidence_status": "pending",
            }
        except Exception as exc:  # candidate failure must not hide a verified L0 write
            # The native API only updates an existing scenario file; a fresh
            # L2 candidate therefore lives in the user-level derived store
            # until the native scene extractor or an explicit reviewer adopts
            # it.  It remains a candidate and is never returned as verified.
            local_path = candidate_store_path(data_root(), path, native.config.identity)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(content, encoding="utf-8")
            candidate = {
                "created": True,
                "status": "candidate",
                "verified": True,
                "backend": "local_pending",
                "native_error": str(exc)[:240],
                "path": path,
                "id": f"autocapture:L2:{path}",
                "evidence_status": "pending",
            }
    elif not native.configured:
        candidate["reason"] = "MemoryCore not configured; no L2 candidate backend"
    elif not l0["l0_verified"]:
        candidate["reason"] = "L0 was not verified"

    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(l0_result.get("ok")) and l0["l0_verified"],
        "idempotency_key": key,
        "source": "openclaw-agent-end",
        "session_id": turn["session_id"],
        "run_id": turn.get("run_id") or None,
        "l0": l0,
        "l1": l1,
        "l2": candidate,
        "l3": {"written": False, "status": "explicit_promotion_only"},
        "memory": native.health(refresh=True, probe_layers=True) if native.configured else l0_result.get("memory"),
        "canonical_repo_changed": False,
    }
    _capture_record(key, result)
    return result


def promote_l3(candidate_id: str) -> dict[str, Any]:
    """Explicitly accept one L2 candidate into the native L3 profile."""

    from autocapture import candidate_store_path

    if not candidate_id or not candidate_id.startswith("autocapture:L2:"):
        raise RuntimeError("promote-l3 only accepts an autocapture L2 candidate id")
    native = native_memory_client()
    if not native.configured:
        raise RuntimeError("MemoryCore is not configured")
    candidate = native.get(candidate_id)
    memory = candidate.get("memory") if isinstance(candidate.get("memory"), dict) else {}
    content = str(memory.get("content") or "").strip()
    if not content:
        raise RuntimeError("candidate has no content")
    accepted = (
        content.replace("status: candidate", "status: accepted", 1)
        .replace("layer: L2", "layer: L3", 1)
        .replace("evidence_status: pending", "evidence_status: accepted", 1)
    )
    current = native.read_core()
    previous = str(current.get("content") or "").strip()
    combined = accepted if not previous else previous + "\n\n" + accepted
    native.write_core(combined)
    verified = native.read_core()
    verified_content = str(verified.get("content") or "")

    relative = candidate_id.split(":", 2)[-1]
    local_path = candidate_store_path(data_root(), relative, native.config.identity)
    # Keep accepted records outside ``candidates`` so the default pending
    # search cannot surface an already-promoted L2 document a second time.
    promoted_path = local_path.parent.parent.parent / "promoted" / local_path.name
    if local_path.is_file():
        promoted_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.replace(promoted_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": accepted in verified_content,
        "candidate": candidate_id,
        "layer": "L3",
        "id": "memorycore:L3:profile",
        "status": "accepted",
        "verified": accepted in verified_content,
        "candidate_archived": str(promoted_path) if promoted_path.is_file() else None,
        "canonical_repo_changed": False,
    }


def _memory_get_result(result_id: str, explain: bool = False) -> dict[str, Any]:
    view = _memory_view()
    adapter = Adapter(None, view)
    memory = adapter.memory_status()
    if memory.get("reachable") is not True:
        return {
            "schema_version": SCHEMA_VERSION,
            "found": False,
            "id": result_id,
            "errors": [{"source": memory.get("backend") or "memorycore", "adapter": memory.get("backend") or "memorycore", "error": "memory backend is unavailable", "freshness": memory}],
            "reason": "memory backend is unavailable",
        }
    try:
        value = adapter.native_memory.get(result_id) if result_id.startswith("autocapture:") else adapter.memory_get(result_id)
    except AdapterError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "found": False,
            "id": result_id,
            "errors": [{"source": memory.get("backend") or "memorycore", "adapter": memory.get("backend") or "memorycore", "error": str(exc), "freshness": memory}],
            "reason": "memory result not found or MemoryCore unavailable",
        }
    backend = "local-memory" if result_id.startswith("autocapture:") else memory.get("backend") or ("memorycore" if adapter.native_memory.configured else "local-memory")
    result = {"schema_version": SCHEMA_VERSION, "found": True, "id": result_id, "source": backend, "repository": None, "commit": None, "result": value, "freshness": memory}
    if explain:
        result["doctor"] = doctor(None)
    return result


def get_result(root: Path | None, result_id: str, explain: bool = False, expected_commit: str | None = None) -> dict[str, Any]:
    errors = []
    if result_id.startswith(("memorycore:", "local:", "autocapture:")):
        return _memory_get_result(result_id, explain)
    try:
        specs = discover_sources(str(root) if root else None)
    except RuntimeError as exc:
        return {"schema_version": SCHEMA_VERSION, "found": False, "id": result_id, "errors": [{"source": None, "adapter": "repository-memory", "error": str(exc), "freshness": None}], "reason": "no repository source configured"}
    for spec in specs:
        view = prepare_view(spec, local=False)
        adapter = discover_adapter(view)
        if expected_commit and view.commit != expected_commit:
            errors.append({"source": spec.id, "adapter": adapter.name, "error": "source commit changed since search", "expected_commit": expected_commit, "current_commit": view.commit, "freshness": view.freshness})
            continue
        canonical_prefix = f"{spec.id}:"
        if result_id.startswith(canonical_prefix):
            relative = normalize_path(result_id.removeprefix(canonical_prefix))
            if relative and ".." not in Path(relative).parts:
                safe_document = _safe_document(view.path, relative)
                if safe_document:
                    _document, content_lines = safe_document
                    # get/explain intentionally returns a larger evidence
                    # window than search, while remaining bounded for giant
                    # generated documents.
                    window = content_lines[:200]
                    start, end = 1, len(window)
                    dirty_local = view.dirty and view.commit_type == "local_worktree"
                    citation = {
                        "source": "repository",
                        "source_id": spec.id,
                        "repository": spec.repository,
                        "commit": view.commit,
                        "commit_type": view.commit_type,
                        "path": relative,
                        "memory_id": result_id,
                        "line_start": start,
                        "line_end": end,
                        "evidence": "\n".join(window),
                        "valid": not dirty_local,
                        "stale": dirty_local,
                        "validation_reason": "source working tree is dirty" if dirty_local else None,
                    }
                    value: dict[str, Any] = {
                        "id": result_id,
                        "kind": "document",
                        "source": spec.id,
                        "repository": spec.repository,
                        "path": relative,
                        "commit": view.commit,
                        "line_start": start,
                        "line_end": end,
                        "excerpt": "\n".join(window),
                        "evidence_window": {"line_start": start, "line_end": end, "truncated": len(content_lines) > len(window)},
                        "support": {"matched_terms": [], "unmatched_terms": [], "coverage": 1.0, "claim_support": "unknown", "supporting_spans": []},
                        "citation": citation,
                        "evidence_status": "stale" if dirty_local else "secondary",
                        "freshness": view.freshness,
                    }
                    result = {"schema_version": SCHEMA_VERSION, "found": True, "id": result_id, "source": spec.id, "repository": spec.repository, "commit": view.commit, "result": value, "freshness": view.freshness}
                    if explain:
                        result["doctor"] = doctor(root, source_id=spec.id)
                    return result
        if not adapter.available:
            continue
        try:
            value = adapter.get(result_id)
            if isinstance(value, dict):
                citation_value = value.get("citation") if isinstance(value.get("citation"), dict) else {}
                backend_path = str(value.get("path") or citation_value.get("path") or "")
                if backend_path and _sensitive_relative_path(backend_path):
                    continue
            result = {"schema_version": SCHEMA_VERSION, "found": True, "id": result_id, "source": spec.id, "repository": spec.repository, "commit": view.commit, "result": value, "freshness": view.freshness}
            if explain:
                result["doctor"] = doctor(root, source_id=spec.id)
            return result
        except AdapterError as exc:
            errors.append({"source": spec.id, "error": str(exc)})
    return {"schema_version": SCHEMA_VERSION, "found": False, "id": result_id, "errors": errors, "reason": "result not found or adapter unavailable"}


def doctor(root: Path | None = None, source_id: str | None = None) -> dict[str, Any]:
    try:
        if root is None and not configured_sources():
            resolve_root()
        specs = discover_sources(str(root) if root else None, source_id)
    except RuntimeError as exc:
        message = str(exc)
        if "no knowledge source configured" not in message and "root not found" not in message:
            raise
        memory_adapter = Adapter(None, _memory_view())
        memory_report = memory_adapter.memory_status()
        if memory_adapter.native_memory.configured:
            memory_report = memory_adapter.native_memory.health(refresh=True, probe_layers=True)
        memory_configured = bool(memory_report.get("configured"))
        memory_ready = memory_report.get("status") == "ready"
        routing = _openclaw_routing()
        capabilities = ["init", "source-add", "memory-init"]
        if memory_configured and memory_ready:
            capabilities.extend(["memory-doctor", "memory-search", "memory-get", "ingest-session"])
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": memory_ready,
            "active_adapter": "native-memorycore" if memory_report.get("backend") != "local-memory" and memory_configured else "local-memory" if memory_configured else None,
            "capabilities": sorted(set(capabilities)),
            "memory": memory_report,
            "repository": {"status": "not_configured", "source_count": 0},
            "knowledge_service": {"configured": False, "required": False, "status": "optional"},
            "semantic": memory_report.get("embedding", {"available": False, "strategy": "keyword-only"}),
            "routing": routing,
            "agents": routing.get("agents", {"configured": [], "covered": []}),
            "sources": [],
            "config": config_summary(),
            "actions": ["init --path <directory>", "source add --path <directory>", "search --scope memory", "ingest-session (explicit write)"] if memory_configured else ["init --path <directory>", "source add --path <directory>", "memorycore configure"],
            "status": "ready" if memory_ready else "not_configured",
            "error": message,
        }
    reports = []
    for spec in specs:
        state = repository_state(spec.root)
        # The checkout directory may be a content-addressed or user-managed
        # snapshot whose basename is not the configured repository identity.
        # Keep doctor/source metadata aligned with the stable source contract.
        state["repository"] = spec.repository
        state["local_only"] = spec.local_only
        # Doctor must describe the same effective read view used by ordinary
        # search: a fresh remote snapshot when one is available.  Keep the
        # canonical worktree state separately in ``state`` so a dirty local
        # checkout is visible without making every agent distrust the clean
        # remote evidence view.
        view = prepare_view(spec, local=False)
        adapter = discover_adapter(view)
        report = adapter_status(adapter, probe_memory_layers=True)
        # Build only the disposable index.  A first-run doctor must not report
        # ``ready`` while leaving the effective repository index absent; this
        # is derived-cache work and never modifies the canonical checkout.
        try:
            ensure_local_index(view)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        snapshot_path = cache_root() / "snapshots" / fingerprint(spec)
        snapshot = {"path": str(snapshot_path), "exists": (snapshot_path / ".git").exists(), "commit": git(snapshot_path, "rev-parse", "HEAD") if (snapshot_path / ".git").exists() else None}
        report.update({"source": spec.id, "repository": spec.repository, "local_only": spec.local_only, "state": state, "index": local_index_status(view), "snapshot_cache": {**snapshot, **view.metadata}, "freshness": view.freshness})
        reports.append(report)
    active = [report.get("name") for report in reports if report.get("available")]
    capabilities = sorted({capability for report in reports for capability in report.get("capabilities", [])})
    def unique_values(values: list[Any]) -> list[Any]:
        unique: list[Any] = []
        seen: set[str] = set()
        for value in values:
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(value)
        return unique

    def memory_fingerprint(value: Any) -> str:
        """Ignore volatile service fields when collapsing shared MemoryCore health."""

        if not isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
        server = normalized.get("server")
        if isinstance(server, dict):
            server.pop("uptime", None)
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)

    def collapse_memory(values: list[Any]) -> list[Any]:
        if not values:
            return []
        first = memory_fingerprint(values[0])
        if all(memory_fingerprint(value) == first for value in values[1:]):
            return [values[0]]
        return unique_values(values)

    semantic = unique_values([report.get("semantic") for report in reports if report.get("semantic")])
    # Every repository report probes the same user-scoped MemoryCore.  Do not
    # turn that one health/data state into a misleading list merely because
    # multiple repository sources are configured.
    memory = collapse_memory([report.get("memory") for report in reports if report.get("memory")])
    actions = ["sync", "search", "use --local"]
    if "ingest-session" in capabilities:
        actions.append("ingest-session (explicit write)")
    native_ready = any(report.get("memory", {}).get("status") == "ready" for report in reports if isinstance(report.get("memory"), dict))
    healthy = all(report.get("healthy", True) for report in reports)
    routing = _openclaw_routing()
    active_values = unique_values(active)
    return {"schema_version": SCHEMA_VERSION, "ok": healthy, "status": "ready" if healthy else "degraded", "active_adapter": active_values[0] if len(active_values) == 1 else active_values, "capabilities": capabilities, "memory": memory[0] if len(memory) == 1 else memory, "repository": {"status": "ready" if reports else "not_configured", "source_count": len(reports)}, "knowledge_service": {"configured": False, "required": False, "status": "optional"}, "semantic": semantic[0] if len(semantic) == 1 else ({"available": False, "strategy": "keyword-only"} if native_ready else semantic), "routing": routing, "agents": routing.get("agents", {"configured": [], "covered": []}), "sources": reports, "config": config_summary(), "actions": actions}


def feedback(root: Path | None, result_id: str, note: str, rating: str | None = None) -> dict[str, Any]:
    destination = data_root() / "feedback.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    item = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "repository": str(root) if root else None, "result_id": result_id, "note": note, "rating": rating}
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return {"schema_version": SCHEMA_VERSION, "written": True, "path": str(destination), "canonical_repo_changed": False, "item": item}


def promote(root: Path, input_path: str) -> dict[str, Any]:
    source = Path(input_path).expanduser()
    text = source.read_text(encoding="utf-8")
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    items = payload if isinstance(payload, list) else [payload]
    destination = data_root() / "candidates"
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("content") or item.get("title") or "").strip():
            raise RuntimeError("promote input items require content or title")
        identifier = re.sub(r"[^a-z0-9-]+", "-", str(item.get("id") or item.get("title") or f"candidate-{index}").lower()).strip("-") or f"candidate-{index}"
        target = destination / f"{identifier}.json"
        candidate = {"schema_version": SCHEMA_VERSION, "id": identifier, "status": "candidate", "evidence_status": "pending", "source": item.get("source"), "citation": item.get("citation"), "title": item.get("title"), "content": item.get("content")}
        target.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(str(target))
    return {"schema_version": SCHEMA_VERSION, "status": "candidate", "written": written, "canonical_repo_changed": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repository-memory")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name)
        child.add_argument("--root", default=argparse.SUPPRESS)
        child.add_argument("--source", default=argparse.SUPPRESS)
        return child

    common("doctor").add_argument("--json", action="store_true")
    sync = common("sync"); sync.add_argument("--deep", action="store_true"); sync.add_argument("--local", action="store_true"); sync.add_argument("--all", action="store_true"); sync.add_argument("--json", action="store_true")
    search_parser = common("search"); search_parser.add_argument("query"); search_parser.add_argument("--limit", type=int, default=5); search_parser.add_argument("--deep", action="store_true"); search_parser.add_argument("--local", action="store_true"); search_parser.add_argument("--scope", choices=("repository", "memory", "all"), default="repository"); search_parser.add_argument("--json", action="store_true")
    get_parser = common("get"); get_parser.add_argument("result_id"); get_parser.add_argument("--commit"); get_parser.add_argument("--json", action="store_true")
    explain_parser = common("explain"); explain_parser.add_argument("result_id"); explain_parser.add_argument("--commit"); explain_parser.add_argument("--json", action="store_true")
    feedback_parser = common("feedback"); feedback_parser.add_argument("result_id"); feedback_parser.add_argument("--note", required=True); feedback_parser.add_argument("--rating"); feedback_parser.add_argument("--json", action="store_true")
    promote_parser = common("promote"); promote_parser.add_argument("--input", required=True); promote_parser.add_argument("--json", action="store_true")
    ingest_parser = common("ingest-session"); ingest_parser.add_argument("--input", required=True); ingest_parser.add_argument("--json", action="store_true")
    capture_parser = common("capture-turn"); capture_parser.add_argument("--input", required=True); capture_parser.add_argument("--json", action="store_true")
    init_parser = sub.add_parser("init"); init_parser.add_argument("--path", required=True); init_parser.add_argument("--id", dest="source_id"); init_parser.add_argument("--repository"); init_parser.add_argument("--profile"); init_parser.add_argument("--local-only", action="store_true"); init_parser.add_argument("--no-sync", action="store_true"); init_parser.add_argument("--json", action="store_true")
    source_parser = sub.add_parser("source"); source_parser.add_argument("action", choices=("add", "list", "remove")); source_parser.add_argument("--path"); source_parser.add_argument("--id", dest="source_id"); source_parser.add_argument("--repository"); source_parser.add_argument("--profile"); source_parser.add_argument("--local-only", action="store_true"); source_parser.add_argument("--no-sync", action="store_true"); source_parser.add_argument("--json", action="store_true")
    evaluate_parser = common("evaluate"); evaluate_parser.add_argument("--queries", required=True); evaluate_parser.add_argument("--qrels", required=True); evaluate_parser.add_argument("--limit", type=int, default=5); evaluate_parser.add_argument("--deep", action="store_true"); evaluate_parser.add_argument("--local", action="store_true"); evaluate_parser.add_argument("--scope", choices=("repository", "memory", "all"), default="repository"); evaluate_parser.add_argument("--revision"); evaluate_parser.add_argument("--fallback-only", action="store_true"); evaluate_parser.add_argument("--json", action="store_true")
    memorycore = sub.add_parser("memorycore")
    memorycore.add_argument("action", choices=["configure", "install", "start", "stop", "status", "promote-l3"])
    memorycore.add_argument("--memorycore-root")
    memorycore.add_argument("--endpoint")
    memorycore.add_argument("--llm-base-url")
    memorycore.add_argument("--model")
    memorycore.add_argument("--state-dir")
    memorycore.add_argument("--team-id")
    memorycore.add_argument("--agent-id")
    memorycore.add_argument("--user-id")
    memorycore.add_argument("--candidate")
    memorycore.add_argument("--accept", action="store_true")
    memorycore.add_argument("--json", action="store_true")
    common("mcp")
    return parser


def _mcp_dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    root = Path(arguments["root"]).expanduser().resolve() if arguments.get("root") else None
    source = arguments.get("source")
    if name == "memory_doctor":
        return doctor(root, source)
    if name == "memory_sync":
        return sync_index(root, source_id=source, local=bool(arguments.get("local")))
    if name == "memory_search":
        return search(root, str(arguments.get("query") or ""), int(arguments.get("limit") or 5), bool(arguments.get("deep")), source, bool(arguments.get("local")), str(arguments.get("scope") or "repository"))
    if name == "memory_get":
        return get_result(root, str(arguments.get("id") or ""), expected_commit=str(arguments.get("commit") or "") or None)
    if name == "memory_init":
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise ValueError("memory_init requires path")
        return init_source(path, str(arguments.get("source_id") or "") or None, str(arguments.get("repository") or "") or None, str(arguments.get("profile") or "") or None, bool(arguments.get("sync", True)), bool(arguments.get("local_only")))
    if name == "memory_ingest":
        if "session" not in arguments:
            raise ValueError("memory_ingest requires session")
        return ingest_session_payload(root, arguments.get("session"), str(arguments.get("source_id") or source or "") or None)
    raise RuntimeError(f"unknown MCP tool: {name}")


def main(argv: list[str] | None = None, forced_command: str | None = None) -> int:
    args = build_parser().parse_args(argv)
    if forced_command:
        args.command = forced_command
    try:
        root_arg = args.root
        if args.command == "mcp":
            configured_root = str(Path(root_arg).expanduser().resolve()) if root_arg else None
            def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                if configured_root and not arguments.get("root"):
                    arguments = {**arguments, "root": configured_root}
                return _mcp_dispatch(name, arguments)
            return serve(dispatch)
        root = None if args.command in {"init", "source", "doctor", "sync", "search", "get", "explain", "feedback", "promote", "ingest-session", "capture-turn"} else resolve_root(root_arg)
        if args.command in {"init", "source"} and root_arg:
            root = resolve_root(root_arg)
        if args.command == "doctor":
            value = doctor(root if root_arg else None, getattr(args, "source", None))
        elif args.command == "sync":
            value = sync_index(root if root_arg else None, args.deep, None if args.all else getattr(args, "source", None), args.local)
        elif args.command == "search":
            value = search(root if root_arg else None, args.query, args.limit, args.deep, getattr(args, "source", None), args.local, args.scope)
        elif args.command == "get":
            value = get_result(root if root_arg else None, args.result_id, expected_commit=args.commit)
        elif args.command == "explain":
            value = get_result(root if root_arg else None, args.result_id, explain=True, expected_commit=args.commit)
        elif args.command == "feedback":
            value = feedback(root, args.result_id, args.note, args.rating)
        elif args.command == "promote":
            value = promote(root, args.input)
        elif args.command == "ingest-session":
            value = ingest_session(root if root_arg else None, args.input, getattr(args, "source", None))
        elif args.command == "capture-turn":
            input_path = Path(args.input).expanduser().resolve()
            raw = input_path.read_text(encoding="utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = [json.loads(line) for line in raw.splitlines() if line.strip()]
            value = capture_turn(root if root_arg else None, payload, getattr(args, "source", None))
        elif args.command == "init":
            value = init_source(args.path, args.source_id, args.repository, args.profile, not args.no_sync, args.local_only)
        elif args.command == "source":
            if args.action == "list":
                value = source_list()
            elif args.action == "add":
                if not args.path:
                    raise RuntimeError("source add requires --path")
                value = init_source(args.path, args.source_id, args.repository, args.profile, not args.no_sync, args.local_only)
            elif args.action == "remove":
                if not args.source_id:
                    raise RuntimeError("source remove requires --id")
                value = remove_source(args.source_id)
            else:
                raise RuntimeError(f"unknown source action: {args.action}")
        elif args.command == "evaluate":
            from evaluate import evaluate_queries

            if args.fallback_only:
                os.environ["REPOSITORY_MEMORY_DISABLE_ADAPTER"] = "1"
            value = evaluate_queries(root, Path(args.queries).expanduser(), Path(args.qrels).expanduser(), limit=args.limit, deep=args.deep, local=args.local, scope=args.scope, revision=args.revision)
        elif args.command == "memorycore":
            if args.action == "promote-l3":
                if not args.accept:
                    raise RuntimeError("promote-l3 requires explicit --accept")
                value = promote_l3(args.candidate or "")
                print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
            from memorycore_service import main as memorycore_main

            service_args = [args.action]
            if args.action == "configure":
                for name in ("memorycore_root", "endpoint", "llm_base_url", "state_dir", "team_id", "agent_id", "user_id"):
                    value = getattr(args, name, None)
                    if value:
                        service_args.extend([f"--{name.replace('_', '-')}", str(value)])
                if args.model:
                    service_args.extend(["--model", args.model])
            return memorycore_main(service_args)
        else:
            raise RuntimeError(f"unknown command: {args.command}")
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeError, TypeError, AdapterError) as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
