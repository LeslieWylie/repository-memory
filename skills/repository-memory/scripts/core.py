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
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
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
from memmy import MemmyError, configure_memmy, memmy_memory_client
from mcp_server import serve
from memorycore import (
    _lifecycle_status as _native_lifecycle_status,
    _with_lifecycle_markers as _with_native_lifecycle,
    native_memory_client,
)
from knowledge import KnowledgeClient
from snapshot import prepare_view
from semantic_repository import ensure as ensure_semantic_index
from semantic_repository import status as semantic_index_status
from semantic_repository import configure as configure_semantic
from semantic_repository import model_status as semantic_model_status
from semantic_repository import summary as semantic_summary
from team_memory import team_memory_store
from vendor_components import report as vendor_components_report

from models import SourceSpec, SourceView

SCHEMA_VERSION = 4
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
    return {"id": view.spec.id, "repository": None if memory_only else view.spec.repository, "root": None if memory_only else str(view.spec.root), "path": None if memory_only else str(view.path), "branch": view.branch, "remote": view.remote_url, "commit": view.commit, "commit_type": view.commit_type, "freshness": _freshness(view)}


def _freshness(view: SourceView) -> dict[str, Any]:
    """Return source freshness without treating a live memory store as stale."""

    value = view.freshness
    if view.commit_type == "memorycore":
        return {**value, "state": "memorycore"}
    return value


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
    home = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or Path.home()).expanduser()
    candidates.append(home / ".openclaw" / "openclaw.json")
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
    guard_status = "advisory" if guard_enabled and guard_ready else "partial" if guard_enabled else "disabled"
    managed_ready = repository_mcp == "ready" and builtin_status == "disabled" and active_status == "disabled" and guard_status == "advisory"
    return {
        "status": "ready" if managed_ready else "degraded",
        "managed": True,
        "repository_mcp": repository_mcp,
        "builtin_memory_search": builtin_status,
        "direct_file_fallback": "audited" if guard_status == "advisory" else "host-dependent",
        "guard_mode": "advisory" if guard_status == "advisory" else "unavailable",
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
    groups = {name: {"verified": [], "answerable": [], "candidates": [], "results": [], "abstain": True} for name in ("repository", "memory")}
    return {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "mode": mode,
        "scope": scope,
        "sources": [_source_payload(view) for view in source_views],
        "verified": [] if scope == "all" else groups[scope]["verified"],
        "answerable": [] if scope == "all" else groups[scope]["answerable"],
        "candidates": [] if scope == "all" else groups[scope]["candidates"],
        "results": [] if scope == "all" else groups[scope]["results"],
        "groups": groups if scope == "all" else None,
        "abstain": True,
        "retrieval_mode": "abstain",
        "freshness": {view.spec.id: _freshness(view) for view in source_views},
        "diagnostics": {"scope": scope, "adapter": backend, "result_count": 0, "retrieval_mode": "abstain", "reason": reason},
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
    memory_backend = citation.get("source") if citation.get("source") in {"memorycore", "standalone-memory", "local-memory", "memmy"} else None
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
    accepted = citation.get("accepted", item.get("accepted"))
    generated = bool(citation.get("generated", item.get("generated", False)))
    if native:
        # Native records have a backend read-back state in addition to the
        # evidence status used by the result splitter.  Keep both explicit so
        # an accepted L2/L3 record cannot be confused with a merely readable
        # generated record.
        if accepted is True:
            lifecycle_status = "accepted"
        elif status in {"candidate", "pending", "inferred", "generated", "stale"}:
            lifecycle_status = status
        elif generated:
            lifecycle_status = "generated"
        else:
            lifecycle_status = "verified"
    else:
        lifecycle_status = status
    linked_evidence = item.get("linked_evidence") or citation.get("linked_evidence") or []
    provenance = item.get("provenance") or citation.get("provenance") or linked_evidence or {
        "source": memory_backend if native else view.spec.id,
        "repository": citation_repository,
        "commit": commit,
        "path": path,
        "locator": citation.get("locator") or ({"start_line": start, "end_line": end} if start else None),
    }
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
        "generated": generated,
        "accepted": accepted,
        "layer": memory_layer,
        "status": lifecycle_status,
        "provenance": provenance,
        "readback": {
            "verified": bool(checked.get("valid")) and not bool(checked.get("stale")),
            "status": "verified" if checked.get("valid") and not checked.get("stale") else ("stale" if checked.get("stale") else "invalid"),
            "source": memory_backend if native else view.spec.id,
            "memory_id": memory_id,
            "layer": memory_layer,
            "receipt": "native-memorycore-readback" if native else "repository-citation-readback",
        },
        "related": item.get("related") or item.get("links") or [],
        "linked_evidence": linked_evidence,
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
            "generated": generated,
            "accepted": accepted,
            "provenance": provenance,
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


def _answerable_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return citations whose excerpt supports the complete query claim.

    ``verified`` is deliberately document-level: it means the citation points
    at a real, fresh, non-pending document.  That is useful for recall and
    qrels evaluation, but it is not permission to answer a composite question.
    Only ``direct`` claim support is answerable without an additional evidence
    lookup.  Partial/unknown hits remain visible as verified documents and
    candidates for diagnostics, while the top-level result abstains.
    """

    return [
        item for item in items
        if str((item.get("support") or {}).get("claim_support") or "") == "direct"
    ]


def _fallback_items(view: SourceView, query: str, limit: int, deep: bool, *, stale: bool = False) -> list[dict[str, Any]]:
    try:
        local_index = ensure_local_index(view, deep)
        view.metadata["local_index"] = local_index
        view.metadata["semantic_index"] = ensure_semantic_index(view, local_index, deep, allow_download=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        # The index is disposable acceleration state; citation-first file
        # scanning remains the safe fallback if cache creation fails.
        view.metadata.pop("local_index", None)
        view.metadata.pop("semantic_index", None)
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

    # A clean local HEAD is still a concrete, line-addressable Git revision.
    # It is not remotely fresh, but it is not stale merely because no remote
    # exists.  Only an uncommitted local worktree is unsafe to verify when the
    # caller did not explicitly request --local.
    return bool(not local and view.dirty and view.commit_type == "local_worktree")


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
        return items, {"source": view.spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "fallback": False, "optional_external_adapter": "unavailable", "memory_skipped": True, "semantic": semantic_summary(view.metadata.get("semantic_index")), "reason": "using the configured local structured repository index"}
    _, sync_error = _sync_if_needed(adapter, view, deep=deep)
    if sync_error:
        items = _fallback_items(view, query, limit, deep, stale=_fallback_is_stale(view, local))
        return items, {"source": view.spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "fallback": False, "optional_external_adapter": adapter.name, "memory_skipped": True, "semantic": semantic_summary(view.metadata.get("semantic_index")), "reason": sync_error}
    try:
        payload = adapter.search(query, limit, deep)
    except AdapterError as exc:
        items = _fallback_items(view, query, limit, deep, stale=_fallback_is_stale(view, local))
        return items, {"source": view.spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "fallback": False, "optional_external_adapter": adapter.name, "memory_skipped": True, "semantic": semantic_summary(view.metadata.get("semantic_index")), "reason": str(exc)}
    normalized = [normalize_item(item, view, source, query) for item, source in _raw_results(payload)]
    return normalized[:limit], {"source": view.spec.id, "adapter": adapter.name, "protocol": adapter.protocol, "fallback": False, "memory_skipped": True, "semantic": semantic_summary(view.metadata.get("semantic_index"))}


def _memory_items(view: SourceView, adapter: Adapter, query: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Search native L0/L1 memory and preserve layer-specific citations."""

    memory = adapter.memory_status()
    if memory.get("reachable") is not True:
        return [], {"source": view.spec.id, "adapter": memory.get("backend") or "memorycore", "memory": memory, "repository_skipped": True, "fallback": False}
    try:
        native_items = adapter.memory_search(query, limit)
    except AdapterError as exc:
        return [], {"source": view.spec.id, "adapter": "memorycore", "memory": memory, "repository_skipped": True, "fallback": True, "reason": str(exc)}
    normalized = [
        normalize_item(item, view, item.get("_memory_backend") or memory.get("backend") or "memorycore", query)
        for item in native_items
    ]
    providers = sorted({str(item.get("source")) for item in normalized if item.get("source")})
    semantic = memory.get("embedding") if isinstance(memory.get("embedding"), dict) else {"available": False, "strategy": "keyword-only"}
    if isinstance(memory.get("providers"), dict):
        memmy = memory["providers"].get("memmy")
        if isinstance(memmy, dict) and isinstance(memmy.get("embedding"), dict) and memmy["embedding"].get("available") is True:
            semantic = memmy["embedding"]
    return normalized[:limit], {
        "source": view.spec.id,
        "adapter": memory.get("backend") or "memorycore",
        "memory": memory,
        "repository_skipped": True,
        "native_memory_count": len(native_items),
        "providers": providers,
        "semantic": semantic,
        "fallback": bool(memory.get("fallback")),
    }


def _package_search(query: str, mode: str, scope: str, views: list[SourceView], groups: dict[str, dict[str, Any]], diagnostics: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    selected = groups[scope] if scope != "all" else {"verified": [], "candidates": [], "results": []}
    memory_ready = any(entry.get("memory", {}).get("status") == "ready" for entry in diagnostics if isinstance(entry.get("memory"), dict))
    semantic_ready = any(
        isinstance(entry.get("semantic"), dict) and entry["semantic"].get("available") is True
        for entry in diagnostics
    )
    semantic_strategies = [
        str(entry["semantic"].get("strategy"))
        for entry in diagnostics
        if isinstance(entry.get("semantic"), dict) and entry["semantic"].get("available") is True
    ]
    retrieval_mode = (
        "grouped"
        if scope == "all"
        else semantic_strategies[0]
        if semantic_strategies
        else "keyword-only"
        if scope == "memory" and memory_ready
        else "lexical"
    )
    result_count = sum(len(group.get("verified", [])) for group in groups.values()) if scope == "all" else len(selected["verified"])
    answerable_count = sum(len(group.get("answerable", [])) for group in groups.values()) if scope == "all" else len(selected.get("answerable", []))
    answerable = [] if scope == "all" else selected.get("answerable", [])[:limit]
    abstain = answerable_count == 0
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
        # ``results`` is the safe answer surface.  Keep ``verified`` separate
        # for document-level retrieval metrics and citation diagnostics.
        "results": answerable,
        "answerable": answerable,
        "groups": groups if scope == "all" else None,
        "abstain": abstain,
        "freshness": {view.spec.id: _freshness(view) for view in views},
        "diagnostics": {
            "scope": scope,
            "adapters": diagnostics,
            "result_count": result_count,
            "answerable_count": answerable_count,
            "candidate_count": candidate_count,
            "claim_abstain": abstain and result_count > 0,
            "retrieval_mode": retrieval_mode,
            "semantic_available": semantic_ready,
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
        group["answerable"] = _answerable_items(group["verified"])
        group["results"] = group["answerable"]
        group["abstain"] = not group["answerable"]
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
            # A normal sync may build the configured derived index, but it
            # must never turn into a model download.  The only operation that
            # may download an optional provider is the explicit
            # ``semantic configure --download`` command.
            semantic = ensure_semantic_index(view, local_index, deep, allow_download=False)
            index_info = {"path": str(local_index_status(view, deep).get("path")), "indexed_commit": local_index.get("commit"), "document_count": len(local_index.get("documents", [])), "deep": deep, "semantic": semantic_summary(semantic)}
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
            results.append({"source": spec.id, "adapter": REPOSITORY_BACKEND, "backend": REPOSITORY_BACKEND, "synced": True, "fallback_ready": True, "repository_index": "local_structured", "reason": "using the configured local structured repository index", "index": index_info, "semantic": index_info.get("semantic"), "memory": adapter.memory_status(), "freshness": view.freshness})
            continue
        try:
            status = adapter.doctor()
            report = status.get("report") if isinstance(status.get("report"), dict) else status
            registered = bool(report.get("wiki") or report.get("registered") or report.get("name")) if isinstance(report, dict) else False
            if not registered and adapter.protocol == "legacy-legacy-memory":
                adapter.add()
            synced = adapter.sync(deep=deep)
            results.append({"source": spec.id, "adapter": adapter.name, "synced": True, "repository_index": "local_structured", "index": index_info, "semantic": index_info.get("semantic"), "freshness": view.freshness, "result": synced})
        except AdapterError as exc:
            results.append({"source": spec.id, "adapter": adapter.name, "synced": False, "adapter_sync": False, "repository_index": "local_structured", "index": index_info, "semantic": index_info.get("semantic"), "fallback_ready": True, "optional_backend": True, "memory": adapter.memory_status(), "freshness": view.freshness, "error": str(exc)})
    return {"schema_version": SCHEMA_VERSION, "sources": results, "canonical_repo_changed": False, "deep": deep, "local": local}


def ingest_session(root: Path | None, input_path: str, source_id: str | None = None) -> dict[str, Any]:
    """Explicitly send a generic session JSON/JSONL payload to the adapter."""
    native = native_memory_client()
    configured_source_adapter: Adapter | None = None
    if root is not None and source_id:
        try:
            specs = discover_sources(str(root), source_id)
            if len(specs) == 1:
                candidate_view = prepare_view(specs[0], local=True)
                candidate_adapter = discover_adapter(candidate_view)
                if candidate_adapter.available and candidate_adapter.protocol == "legacy-legacy-memory":
                    configured_source_adapter = candidate_adapter
        except (RuntimeError, OSError, ValueError):
            configured_source_adapter = None
    if configured_source_adapter is not None:
        view = configured_source_adapter.source
        adapter = configured_source_adapter
        source_name = source_id or adapter.source.spec.id
    elif native.configured:
        view = _memory_view()
        adapter = Adapter(None, view)
        native_backend = getattr(native, "backend", None)
        source_name = native_backend if isinstance(native_backend, str) else "memorycore"
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


def memmy_gui(open_window: bool = False) -> dict[str, Any]:
    """Expose the existing Memmy panel without creating a second GUI."""

    client = memmy_memory_client()
    health = client.health()
    endpoint = client.config.endpoint
    if not endpoint:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "not_configured",
            "error": "Memmy is not configured; run repository-memory memmy configure",
            "canonical_repo_changed": False,
        }
    url = endpoint.rstrip("/") + "/"
    opened = False
    open_error = None
    if open_window:
        if sys.platform != "darwin":
            open_error = "--open is currently supported only on macOS"
        else:
            try:
                subprocess.run(["open", url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                opened = True
            except (OSError, subprocess.CalledProcessError) as exc:
                open_error = str(exc)[:240]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": health.get("reachable") is True and open_error is None,
        "backend": "memmy",
        "url": url,
        "reachable": health.get("reachable") is True,
        "health": health,
        "opened": opened,
        "error": open_error,
        "canonical_repo_changed": False,
    }


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


def project_memory_candidates() -> dict[str, Any]:
    """Create reviewable L2 candidates from the standalone L0 conversation store."""

    native = native_memory_client()
    projector = getattr(native, "project_candidates", None)
    if not callable(projector):
        raise RuntimeError("memory project requires the built-in standalone runtime")
    return projector()


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
    asynchronously.  Native L2 is produced by the MemoryCore pipeline and is
    only reported when a scenario is read back.  L3 is intentionally untouched
    and can only be changed by an explicit promotion.
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

    native_scenarios_before: dict[str, str] = {}
    native_accepted_before: dict[str, str] = {}
    if native.configured:
        try:
            native_scenarios_before = native.scenario_snapshot()
            for scenario_path in native_scenarios_before:
                record = native.read_scenario(scenario_path)
                content = str(record.get("content") or "")
                if _native_lifecycle_status(content, "generated") == "accepted":
                    native_accepted_before[scenario_path] = content
        except Exception:
            native_scenarios_before = {}
            native_accepted_before = {}
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

    candidate: dict[str, Any] = {"created": False, "status": "skipped", "reason": "not durable", "backend": "native-pipeline"}
    if should_create_candidate(turn) and native.configured and l0["l0_verified"]:
        # Do not call scenario/write here: the native endpoint is update-only
        # and a fabricated local file is not an L2 memory.  L2 creation belongs
        # to the MemoryCore scene extractor triggered by conversation/add.
        observed = native.wait_for_scenario(native_scenarios_before, timeout=float(os.environ.get("REPOSITORY_MEMORY_L2_WAIT", "8")))
        if observed:
            path = str(observed.get("path") or "")
            observed_content = str((observed.get("record") or {}).get("content") or "")
            accepted_before = native_accepted_before.get(path)
            if accepted_before and observed.get("status") != "accepted" and observed_content != accepted_before:
                # The native pipeline is allowed to propose an update, but it
                # must not silently revoke an explicitly accepted scenario.
                # Preserve the accepted native record and put the new turn in
                # the normal user-level pending candidate store instead.
                native.write_scenario(path, accepted_before, summary="Preserved accepted repository-memory L2 scenario")
                restored = native.read_scenario(path)
                if _native_lifecycle_status(str(restored.get("content") or ""), "generated") != "accepted":
                    raise RuntimeError("accepted native L2 scenario could not be restored after pipeline update")
                relative_candidate = candidate_path(turn)
                candidate_file = candidate_store_path(data_root(), relative_candidate, identity)
                candidate_file.parent.mkdir(parents=True, exist_ok=True)
                candidate_file.write_text(candidate_markdown(turn, l0, l1), encoding="utf-8")
                candidate = {
                    "created": True,
                    "status": "pending",
                    "verified": False,
                    "backend": "local-candidate",
                    "path": relative_candidate,
                    "id": f"autocapture:L2:{relative_candidate}",
                    "evidence_status": "pending",
                    "native_path": path,
                    "native_update_preserved": True,
                    "reason": "native pipeline proposed an update to an accepted scenario; explicit re-review is required",
                }
            else:
                candidate = {
                    "created": True,
                    "status": "candidate" if observed.get("status") != "accepted" else "accepted",
                    "verified": bool(observed_content),
                    "backend": getattr(native, "backend", "memorycore"),
                    "path": path,
                    "id": f"{getattr(native, 'backend', 'memorycore')}:L2:{path}",
                    "evidence_status": observed.get("status") or "generated",
                }
        else:
            candidate = {
                "created": False,
                "status": "pending",
                "backend": "native-pipeline",
                "reason": "native scene extraction has not produced a read-back scenario within the observation window",
                "evidence_status": "pending",
            }
    elif not native.configured:
        candidate["backend"] = "native-pipeline"
        candidate["reason"] = "MemoryCore not configured; native L2 pipeline unavailable"
    elif not l0["l0_verified"]:
        candidate["reason"] = "L0 was not verified"

    # Shared Team Memory is intentionally narrower than raw conversation
    # capture.  A durable answer becomes a reviewable candidate only when it
    # contains a reusable decision, failure, discovery, solution, or handoff.
    team_candidate: dict[str, Any] = {"created": False, "status": "skipped", "reason": "not durable"}
    if should_create_candidate(turn):
        answer = next((item["content"] for item in reversed(turn["messages"]) if item["role"] == "assistant"), "")
        if re.search(r"决定|选择|采用|策略|decision|policy|选择", answer, re.IGNORECASE):
            memory_type = "decision"
        elif re.search(r"失败|报错|异常|阻塞|原因|failure|error|blocked|root cause", answer, re.IGNORECASE):
            memory_type = "failure"
        elif re.search(r"修复|解决|方案|workaround|fix|solution|resolved", answer, re.IGNORECASE):
            memory_type = "solution"
        elif re.search(r"交接|下一步|待办|handoff|next step|follow[- ]?up", answer, re.IGNORECASE):
            memory_type = "handoff"
        else:
            memory_type = "discovery"
        try:
            team_result = team_memory_store().publish({
                "type": memory_type,
                "title": answer[:140].splitlines()[0] if answer else memory_type,
                "content": answer,
                "scope": {"workspace": Path(turn.get("workspace") or "").name if turn.get("workspace") else None},
                "provenance": {"agent": turn.get("agent_id"), "session": turn.get("session_id"), "run_id": turn.get("run_id")},
                "confidence": 0.4,
                "status": "candidate",
                "idempotency_key": f"capture:{key}",
            })
            team_memory = team_result.get("memory") if isinstance(team_result.get("memory"), dict) else {}
            team_candidate = {"created": True, "status": "candidate", "id": team_memory.get("id"), "memory_type": memory_type, "duplicate": team_result.get("duplicate", False), "evidence_status": "candidate"}
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            team_candidate = {"created": False, "status": "error", "reason": str(exc)[:240]}

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
        "team_memory": team_candidate,
        "l3": {"written": False, "status": "explicit_promotion_only"},
        "memory": native.health(refresh=True, probe_layers=True) if native.configured else l0_result.get("memory"),
        "canonical_repo_changed": False,
    }
    _capture_record(key, result)
    return result


def promote_l3(candidate_id: str) -> dict[str, Any]:
    """Explicitly accept one native L2 scenario and write/read back L3.

    The operation is intentionally idempotent.  Re-running promotion for an
    already accepted scenario must not append another copy of the same L2
    block to the L3 profile.
    """

    if not candidate_id or not re.match(r"^(?:memorycore|standalone-memory|local):L2:", candidate_id):
        raise RuntimeError("promote-l3 requires an L2 scenario id; local pending candidates are not promotable")
    native = native_memory_client()
    if not native.configured:
        raise RuntimeError("MemoryCore is not configured")
    path = candidate_id.split(":", 2)[-1]
    candidate = native.get(candidate_id)
    memory = candidate.get("memory") if isinstance(candidate.get("memory"), dict) else {}
    if getattr(native, "backend", "") == "standalone-memory":
        try:
            metadata = json.loads(str(memory.get("metadata") or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        path = str(metadata.get("path") or path)
    content = str(memory.get("content") or "").strip()
    if not content:
        raise RuntimeError("native L2 scenario has no content")
    accepted_l2 = _with_native_lifecycle(content, status="accepted", layer="L2", source_l2=path)
    native.write_scenario(path, accepted_l2, summary="Explicitly accepted repository-memory L2 scenario")
    l2_readback = native.read_scenario(path)
    l2_content = str(l2_readback.get("content") or "")
    if _native_lifecycle_status(l2_content) != "accepted":
        raise RuntimeError("native L2 scenario write did not read back as accepted")

    accepted = _with_native_lifecycle(
        "# Repository Memory Profile\n\n" + l2_content,
        status="accepted",
        layer="L3",
        source_l2=path,
    )
    current = native.read_core()
    previous = str(current.get("content") or "").strip()
    marker = f"source_l2: {path}"
    if marker not in previous:
        combined = accepted if not previous else previous + "\n\n" + accepted
        native.write_core(combined)
    verified = native.read_core()
    verified_content = str(verified.get("content") or "")
    if _native_lifecycle_status(verified_content) != "accepted" or path not in verified_content:
        raise RuntimeError("native L3 core write did not read back as accepted")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "candidate": candidate_id,
        "l2": {"id": candidate_id, "path": path, "status": "accepted", "readback": True},
        "layer": "L3",
        "id": f"{getattr(native, 'backend', 'memorycore')}:L3:profile",
        "status": "accepted",
        "verified": True,
        "readback": True,
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
    backend = "memmy" if result_id.startswith("memmy:") else "local-memory" if result_id.startswith("autocapture:") else memory.get("backend") or (getattr(adapter.native_memory, "backend", "memorycore") if adapter.native_memory.configured else "local-memory")
    raw_layer = value.get("layer") or value.get("memoryLayer") if isinstance(value, dict) else None
    if not raw_layer:
        match = re.match(r"^(?:memorycore|standalone-memory|memmy|local|autocapture):(L[0-3]|Skill):", result_id)
        raw_layer = match.group(1) if match else None
    layer = str(raw_layer or "") or None
    payload = value.get("memory") if isinstance(value, dict) and isinstance(value.get("memory"), dict) else (value if isinstance(value, dict) else {})
    content = str(payload.get("content") or payload.get("body") or payload.get("text") or payload.get("excerpt") or payload.get("summary") or "")
    citation = value.get("citation") if isinstance(value, dict) and isinstance(value.get("citation"), dict) else {}
    memory_id = str(citation.get("memory_id") or payload.get("id") or (result_id.split(":", 2)[-1] if ":" in result_id else result_id))
    memory_path = citation.get("path") or payload.get("path")
    if not memory_path and layer == "L2":
        memory_path = f"scenario/{memory_id}"
    if not memory_path and layer == "L3":
        memory_path = "core/profile"
    accepted = bool(
        citation.get("accepted") is True
        or payload.get("accepted") is True
        or payload.get("status") in {"activated", "active", "accepted"}
        or _native_lifecycle_status(content, "") == "accepted"
    )
    generated = bool(citation.get("generated") is True or payload.get("generated") is True or _native_lifecycle_status(content, "") == "generated")
    if layer in {"L2", "L3"}:
        status = "accepted" if accepted else (_native_lifecycle_status(content, "generated" if generated else "pending"))
    else:
        status = "verified"
    provenance = value.get("provenance") if isinstance(value, dict) else None
    provenance = provenance or citation.get("provenance") or value.get("linked_evidence") if isinstance(value, dict) else None
    provenance = provenance or {
        "source": backend,
        "repository": None,
        "commit": None,
        "path": memory_path,
        "locator": citation.get("locator"),
    }
    readback = {
        "verified": True,
        "status": "verified",
        "source": backend,
        "memory_id": memory_id,
        "layer": layer,
        "receipt": "standalone-memory-readback" if backend == "standalone-memory" else "native-memorycore-readback" if backend == "memorycore" else "memmy-readback" if backend == "memmy" else "local-memory-readback",
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "found": True,
        "id": result_id,
        "source": backend,
        "repository": None,
        "commit": None,
        "layer": layer,
        "memory_id": memory_id,
        "status": status,
        "generated": generated,
        "accepted": accepted,
        "citation": citation or {
            "source": backend,
            "memory_id": memory_id,
            "layer": layer,
            "evidence": content,
            "locator": {"memory_id": memory_id},
            "valid": bool(content),
            "accepted": accepted,
            "generated": generated,
        },
        "provenance": provenance,
        "readback": readback,
        "result": value,
        "freshness": memory,
    }
    if explain:
        result["doctor"] = doctor(None)
    return result


def get_result(
    root: Path | None,
    result_id: str,
    explain: bool = False,
    expected_commit: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
) -> dict[str, Any]:
    if result_id.startswith("team:"):
        value = team_memory_store().get(result_id)
        if explain:
            value["doctor"] = doctor(root)
        return value
    errors = []
    if result_id.startswith(("memorycore:", "standalone-memory:", "memmy:", "local:", "autocapture:")):
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
                    # A search result can pass its citation locator back to
                    # get/explain.  Prefer that exact evidence window so a
                    # large standup/report does not silently return lines
                    # 1-200 for a hit at line 455.  Without a locator retain
                    # the bounded first-window compatibility behavior.
                    requested_start = max(1, int(line_start or 1))
                    requested_end = max(requested_start, int(line_end or min(200, len(content_lines))))
                    if line_start is not None or line_end is not None:
                        start = min(requested_start, len(content_lines) or 1)
                        end = min(requested_end, len(content_lines) or start)
                        window = content_lines[start - 1:end]
                    else:
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
                        "evidence_window": {"line_start": start, "line_end": end, "requested_line_start": line_start, "requested_line_end": line_end, "truncated": len(content_lines) > len(window)},
                        "support": {"matched_terms": [], "unmatched_terms": [], "coverage": 1.0, "claim_support": "unknown", "supporting_spans": []},
                        "citation": citation,
                        "evidence_status": "stale" if dirty_local else "secondary",
                        "freshness": view.freshness,
                        "layer": "repository",
                        "status": "stale" if dirty_local else "verified",
                        "provenance": {
                            "source": spec.id,
                            "repository": spec.repository,
                            "commit": view.commit,
                            "path": relative,
                            "locator": {"start_line": start, "end_line": end},
                        },
                        "readback": {
                            "verified": not dirty_local,
                            "status": "stale" if dirty_local else "verified",
                            "source": spec.id,
                            "memory_id": result_id,
                            "layer": "repository",
                            "receipt": "repository-citation-readback",
                        },
                    }
                    result = {"schema_version": SCHEMA_VERSION, "found": True, "id": result_id, "source": spec.id, "repository": spec.repository, "commit": view.commit, "layer": "repository", "memory_id": result_id, "status": "stale" if dirty_local else "verified", "provenance": value["provenance"], "readback": value["readback"], "result": value, "freshness": view.freshness}
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
            native_report = memory_adapter.native_memory.health(refresh=True, probe_layers=True)
            memory_report = native_report
            if memory_adapter.memmy.configured:
                memmy_report = memory_adapter.memmy.health()
                memory_report["providers"] = {
                    "memorycore": {**native_report, "providers": None},
                    "memmy": memmy_report,
                }
        provider_reports = memory_report.get("providers") if isinstance(memory_report.get("providers"), dict) else {}
        provider_configured = any(
            isinstance(report, dict) and report.get("configured") is True
            for report in provider_reports.values()
        )
        provider_ready = any(
            isinstance(report, dict) and report.get("status") == "ready"
            for report in provider_reports.values()
        )
        memory_configured = bool(memory_report.get("configured") or provider_configured)
        memory_ready = memory_report.get("status") == "ready" or provider_ready
        native_ready = bool(memory_adapter.native_memory.configured and memory_report.get("status") == "ready")
        team_report = team_memory_store().health()
        routing = _openclaw_routing()
        capabilities = ["init", "source-add", "memory-init"]
        if memory_configured and memory_ready:
            capabilities.extend(["memory-doctor", "memory-search", "memory-get", "ingest-session"])
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": memory_ready,
            "active_adapter": memory_report.get("backend") if memory_ready else "memmy" if isinstance(provider_reports.get("memmy"), dict) and provider_reports["memmy"].get("status") == "ready" else None,
            "capabilities": sorted(set(capabilities)),
            "memory": memory_report,
            "team_memory": team_report,
            "repository": {"status": "not_configured", "source_count": 0},
            "knowledge_service": {"required": False, **KnowledgeClient().health()},
            "semantic": memory_report.get("embedding", {"available": False, "strategy": "keyword-only"}),
            "routing": routing,
            "agents": routing.get("agents", {"configured": [], "covered": []}),
            "sources": [],
            "config": config_summary(),
            "upstream_components": vendor_components_report(),
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
            repository_semantic = semantic_index_status(view)
        except (OSError, RuntimeError, TypeError, ValueError):
            repository_semantic = {"configured": False, "available": False, "strategy": "lexical", "error": "repository semantic index probe failed"}
        snapshot_path = cache_root() / "snapshots" / fingerprint(spec)
        snapshot = {"path": str(snapshot_path), "exists": (snapshot_path / ".git").exists(), "commit": git(snapshot_path, "rev-parse", "HEAD") if (snapshot_path / ".git").exists() else None}
        report.update({"source": spec.id, "repository": spec.repository, "local_only": spec.local_only, "state": state, "index": {**local_index_status(view), "semantic": repository_semantic}, "repository_semantic": repository_semantic, "snapshot_cache": {**snapshot, **view.metadata}, "freshness": view.freshness})
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
    return {"schema_version": SCHEMA_VERSION, "ok": healthy, "status": "ready" if healthy else "degraded", "active_adapter": active_values[0] if len(active_values) == 1 else active_values, "capabilities": capabilities, "memory": memory[0] if len(memory) == 1 else memory, "team_memory": team_memory_store().health(), "repository": {"status": "ready" if reports else "not_configured", "source_count": len(reports)}, "knowledge_service": {"required": False, **KnowledgeClient().health()}, "semantic": semantic[0] if len(semantic) == 1 else ({"available": False, "strategy": "keyword-only"} if native_ready else semantic), "routing": routing, "agents": routing.get("agents", {"configured": [], "covered": []}), "sources": reports, "config": config_summary(), "upstream_components": vendor_components_report(), "actions": actions}


def feedback(root: Path | None, result_id: str, note: str, rating: str | None = None, feedback_id: str | None = None) -> dict[str, Any]:
    if result_id.startswith("team:"):
        return team_memory_store().feedback(result_id, rating or "helpful", note, feedback_id=feedback_id)
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
    native_candidates = []
    native = native_memory_client()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("content") or item.get("title") or "").strip():
            raise RuntimeError("promote input items require content or title")
        identifier = re.sub(r"[^a-z0-9-]+", "-", str(item.get("id") or item.get("title") or f"candidate-{index}").lower()).strip("-") or f"candidate-{index}"
        target = destination / f"{identifier}.json"
        candidate = {"schema_version": SCHEMA_VERSION, "id": identifier, "status": "candidate", "evidence_status": "pending", "source": item.get("source"), "citation": item.get("citation"), "title": item.get("title"), "content": item.get("content")}
        target.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(str(target))
        if getattr(native, "backend", "") == "standalone-memory":
            scenario = native.write_scenario(identifier, str(item.get("content") or item.get("title") or ""), summary=str(item.get("title") or identifier))
            native_candidates.append({"id": scenario.get("id"), "path": identifier, "status": scenario.get("status"), "readback": bool(scenario.get("content"))})
    return {"schema_version": SCHEMA_VERSION, "status": "candidate", "written": written, "native_l2": native_candidates, "canonical_repo_changed": False}


def _read_json_or_jsonl(input_path: str) -> Any:
    source = Path(input_path).expanduser()
    text = source.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return rows


def publish_memory(input_path: str, *, status: str | None = None) -> dict[str, Any]:
    """Explicitly publish one or more cross-agent memories."""

    payload = _read_json_or_jsonl(input_path)
    items = payload if isinstance(payload, list) else [payload]
    written = []
    store = team_memory_store()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("memory_publish input must contain JSON objects")
        written.append(store.publish({**item, **({"status": status} if status else {})}))
    return {"schema_version": SCHEMA_VERSION, "ok": True, "published": written, "count": len(written), "canonical_repo_changed": False}


def activate_memory(memory_id: str, reviewer: str | None = None) -> dict[str, Any]:
    """Explicitly move one Team Memory candidate into the active plane."""

    result = team_memory_store().activate(memory_id, reviewer=reviewer)
    result["schema_version"] = SCHEMA_VERSION
    result["write_operation"] = "explicit-review"
    return result


def export_team_memory(output_path: str) -> dict[str, Any]:
    """Export the user-level Team Memory plane as an explicit sync bundle."""

    destination = Path(output_path).expanduser().resolve()
    bundle = team_memory_store().export_bundle()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return {"schema_version": SCHEMA_VERSION, "ok": True, "operation": "team-memory-export", "path": str(destination), "records": len(bundle.get("records", [])), "feedback": len(bundle.get("feedback", [])), "canonical_repo_changed": False}


def import_team_memory(input_path: str) -> dict[str, Any]:
    """Merge an explicit Team Memory bundle into the configured backend."""

    source = Path(input_path).expanduser().resolve()
    try:
        bundle = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Team Memory bundle: {source}") from exc
    result = team_memory_store().import_bundle(bundle)
    result.update({"operation": "team-memory-import", "path": str(source)})
    return result


def supersede_memory(memory_id: str, input_path: str) -> dict[str, Any]:
    store = team_memory_store()
    store.get(memory_id)
    payload = _read_json_or_jsonl(input_path)
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError("memory_supersede input must contain exactly one object")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("memory_supersede input must contain one JSON object")
    payload = {**payload, "status": "active", "supersedes": memory_id}
    result = store.publish(payload, default_status="active")
    return {"schema_version": SCHEMA_VERSION, "ok": True, "superseded": memory_id, "replacement": result, "canonical_repo_changed": False}


def memory_context(
    root: Path | None,
    query: str,
    *,
    limit: int = 5,
    source_id: str | None = None,
    repo: str | None = None,
    issue: str | None = None,
    branch: str | None = None,
    agent: str | None = None,
    local: bool = False,
) -> dict[str, Any]:
    """Build one context package without erasing provenance boundaries.

    Repository retrieval and Team Memory retrieval are ranked in their own
    planes.  The package is the fusion seam: it gives the agent the sections it
    needs while keeping Git citations distinct from experience/decision
    provenance.  No cross-backend score is invented.
    """

    # Query normalization is shared, but the two retrieval lanes run in
    # parallel.  They remain separate after recall: scores and provenance are
    # never mixed into an opaque cross-backend ranking.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-context") as pool:
        repository_future = pool.submit(search, root, query, limit, False, source_id, local, "repository")
        team_future = pool.submit(team_memory_store().search, query, limit=limit, repo=repo, issue=issue, branch=branch, agent=agent)
        repository = repository_future.result()
        team = team_future.result()
    active = team.get("active", []) if isinstance(team, dict) else []
    candidates = team.get("candidates", []) if isinstance(team, dict) else []
    grouped: dict[str, list[dict[str, Any]]] = {memory_type: [] for memory_type in ("evidence", "decision", "discovery", "failure", "solution", "handoff")}
    for item in active:
        grouped.setdefault(str(item.get("memory_type") or "discovery"), []).append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "mode": classify(query),
        "retrieval_mode": "multi-source-lexical",
        "retrieval_strategy": "sectioned-lexical",
        "semantic_available": False,
        "abstain": not repository.get("verified") and not active,
        "context": {
            "repository_evidence": repository.get("verified", []),
            "repository_candidates": repository.get("candidates", []),
            "team_memory": active,
            "decisions": grouped.get("decision", []),
            "failures": grouped.get("failure", []),
            "solutions": grouped.get("solution", []),
            "discoveries": grouped.get("discovery", []),
            "handoffs": grouped.get("handoff", []),
            "evidence_memory": grouped.get("evidence", []),
            "team_candidates": candidates,
        },
        "freshness": repository.get("freshness", {}),
        "diagnostics": {
            "fusion": "sectioned-provenance; repository and team scores are not mixed",
            "parallel_recall": True,
            "repository_verified": len(repository.get("verified", [])),
            "repository_candidates": len(repository.get("candidates", [])),
            "team_active": len(active),
            "team_candidates": len(candidates),
            "team_memory": team.get("diagnostics", {}),
            "repository": repository.get("diagnostics", {}),
            "semantic_available": False,
        },
        "sources": {"repository": repository, "team_memory": {**team_memory_store().health(), "retrieval_mode": "lexical"}},
    }


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
    get_parser = common("get"); get_parser.add_argument("result_id"); get_parser.add_argument("--commit"); get_parser.add_argument("--line-start", type=int); get_parser.add_argument("--line-end", type=int); get_parser.add_argument("--json", action="store_true")
    explain_parser = common("explain"); explain_parser.add_argument("result_id"); explain_parser.add_argument("--commit"); explain_parser.add_argument("--line-start", type=int); explain_parser.add_argument("--line-end", type=int); explain_parser.add_argument("--json", action="store_true")
    feedback_parser = common("feedback"); feedback_parser.add_argument("result_id"); feedback_parser.add_argument("--note", required=True); feedback_parser.add_argument("--rating"); feedback_parser.add_argument("--feedback-id"); feedback_parser.add_argument("--json", action="store_true")
    promote_parser = common("promote"); promote_parser.add_argument("--input", required=True); promote_parser.add_argument("--json", action="store_true")
    publish_parser = common("publish"); publish_parser.add_argument("--input", required=True); publish_parser.add_argument("--status", choices=("candidate", "active"), default="candidate"); publish_parser.add_argument("--json", action="store_true")
    activate_parser = common("team-activate"); activate_parser.add_argument("--id", required=True); activate_parser.add_argument("--reviewer"); activate_parser.add_argument("--json", action="store_true")
    export_parser = common("team-export"); export_parser.add_argument("--output", required=True); export_parser.add_argument("--json", action="store_true")
    import_parser = common("team-import"); import_parser.add_argument("--input", required=True); import_parser.add_argument("--json", action="store_true")
    context_parser = common("context"); context_parser.add_argument("query"); context_parser.add_argument("--limit", type=int, default=5); context_parser.add_argument("--repo"); context_parser.add_argument("--issue"); context_parser.add_argument("--branch"); context_parser.add_argument("--agent"); context_parser.add_argument("--local", action="store_true"); context_parser.add_argument("--json", action="store_true")
    supersede_parser = common("supersede"); supersede_parser.add_argument("--id", required=True); supersede_parser.add_argument("--input", required=True); supersede_parser.add_argument("--json", action="store_true")
    ingest_parser = common("ingest-session"); ingest_parser.add_argument("--input", required=True); ingest_parser.add_argument("--json", action="store_true")
    capture_parser = common("capture-turn"); capture_parser.add_argument("--input", required=True); capture_parser.add_argument("--json", action="store_true")
    init_parser = sub.add_parser("init"); init_parser.add_argument("--path", required=True); init_parser.add_argument("--id", dest="source_id"); init_parser.add_argument("--repository"); init_parser.add_argument("--profile"); init_parser.add_argument("--local-only", action="store_true"); init_parser.add_argument("--no-sync", action="store_true"); init_parser.add_argument("--json", action="store_true")
    source_parser = sub.add_parser("source"); source_parser.add_argument("action", choices=("add", "list", "remove")); source_parser.add_argument("--path"); source_parser.add_argument("--id", dest="source_id"); source_parser.add_argument("--repository"); source_parser.add_argument("--profile"); source_parser.add_argument("--local-only", action="store_true"); source_parser.add_argument("--no-sync", action="store_true"); source_parser.add_argument("--json", action="store_true")
    evaluate_parser = common("evaluate"); evaluate_parser.add_argument("--queries", required=True); evaluate_parser.add_argument("--qrels", required=True); evaluate_parser.add_argument("--limit", type=int, default=5); evaluate_parser.add_argument("--deep", action="store_true"); evaluate_parser.add_argument("--local", action="store_true"); evaluate_parser.add_argument("--scope", choices=("repository", "memory", "all"), default="repository"); evaluate_parser.add_argument("--revision"); evaluate_parser.add_argument("--fallback-only", action="store_true"); evaluate_parser.add_argument("--json", action="store_true")
    team_evaluate_parser = common("team-evaluate"); team_evaluate_parser.add_argument("--records", required=True); team_evaluate_parser.add_argument("--queries", required=True); team_evaluate_parser.add_argument("--qrels", required=True); team_evaluate_parser.add_argument("--limit", type=int, default=5); team_evaluate_parser.add_argument("--gate", action="store_true"); team_evaluate_parser.add_argument("--min-p1", type=float, default=1.0); team_evaluate_parser.add_argument("--min-recall", type=float, default=1.0); team_evaluate_parser.add_argument("--min-negative", type=float, default=1.0); team_evaluate_parser.add_argument("--max-candidate-contamination", type=float, default=0.0); team_evaluate_parser.add_argument("--json", action="store_true")
    compact_parser = common("team-compact"); compact_parser.add_argument("--keep", type=int, default=1); compact_parser.add_argument("--json", action="store_true")
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
    memorycore.add_argument("--pipeline-mode", choices=("native", "fast"))
    memorycore.add_argument("--candidate")
    memorycore.add_argument("--accept", action="store_true")
    memorycore.add_argument("--json", action="store_true")
    memory = sub.add_parser("memory", help="Inspect and explicitly operate the standalone local memory runtime")
    memory.add_argument("action", choices=["status", "project", "promote-l3"])
    memory.add_argument("--candidate")
    memory.add_argument("--accept", action="store_true")
    memory.add_argument("--json", action="store_true")
    knowledge = sub.add_parser("knowledge")
    knowledge.add_argument("action", choices=("status", "configure", "install", "start", "stop", "create", "sync", "search"))
    knowledge.add_argument("--root", default=argparse.SUPPRESS)
    knowledge.add_argument("--source")
    knowledge.add_argument("--wiki-id")
    knowledge.add_argument("--endpoint")
    knowledge.add_argument("--port", type=int)
    knowledge.add_argument("--state-dir")
    knowledge.add_argument("--service-id")
    knowledge.add_argument("--team-id")
    knowledge.add_argument("--user-id")
    knowledge.add_argument("--agent-id")
    knowledge.add_argument("--node-modules")
    knowledge.add_argument("--name")
    knowledge.add_argument("--query")
    knowledge.add_argument("--limit", type=int, default=5)
    knowledge.add_argument("--deep", action="store_true")
    knowledge.add_argument("--json", action="store_true")
    memmy = sub.add_parser("memmy")
    memmy.add_argument("action", choices=("status", "configure", "search"))
    memmy.add_argument("--endpoint")
    memmy.add_argument("--profile-id")
    memmy.add_argument("--user-id")
    memmy.add_argument("--query")
    memmy.add_argument("--limit", type=int, default=5)
    memmy.add_argument("--json", action="store_true")
    gui = sub.add_parser("gui")
    gui.add_argument("--open", action="store_true")
    gui.add_argument("--json", action="store_true")
    semantic = sub.add_parser("semantic", help="Configure the optional local Hugging Face repository encoder")
    semantic.add_argument("action", choices=("status", "configure"))
    semantic.add_argument("--model", default="Alibaba-NLP/gte-multilingual-base")
    semantic.add_argument("--download", action="store_true", help="Allow the explicit configure operation to download model files")
    semantic.add_argument("--disable", action="store_true")
    semantic.add_argument("--json", action="store_true")
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
        return get_result(
            root,
            str(arguments.get("id") or ""),
            expected_commit=str(arguments.get("commit") or "") or None,
            line_start=int(arguments["line_start"]) if arguments.get("line_start") is not None else None,
            line_end=int(arguments["line_end"]) if arguments.get("line_end") is not None else None,
        )
    if name == "memory_context":
        return memory_context(root, str(arguments.get("query") or ""), limit=int(arguments.get("limit") or 5), source_id=source, repo=str(arguments.get("repo") or "") or None, issue=str(arguments.get("issue") or "") or None, branch=str(arguments.get("branch") or "") or None, agent=str(arguments.get("agent") or "") or None, local=bool(arguments.get("local")))
    if name == "memory_team_sync":
        mode = str(arguments.get("mode") or "status").lower()
        if mode == "export":
            output = str(arguments.get("output") or "").strip()
            if not output:
                raise ValueError("memory_team_sync export requires output")
            return export_team_memory(output)
        if mode == "import":
            source_path = str(arguments.get("input") or "").strip()
            if not source_path:
                raise ValueError("memory_team_sync import requires input")
            return import_team_memory(source_path)
        return {"schema_version": SCHEMA_VERSION, "ok": True, "operation": "team-memory-status", "backend": team_memory_store().health(), "canonical_repo_changed": False}
    if name == "memory_team_activate":
        return activate_memory(str(arguments.get("id") or ""), str(arguments.get("reviewer") or "") or None)
    if name == "memory_publish":
        if "memory" not in arguments:
            raise ValueError("memory_publish requires memory")
        payload = arguments.get("memory")
        data_dir = data_root() / "incoming"
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="team-memory-", dir=data_dir, delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False)
            path = Path(handle.name)
        try:
            return publish_memory(str(path), status=str(arguments.get("status") or "candidate"))
        finally:
            path.unlink(missing_ok=True)
    if name == "memory_feedback":
        return feedback(root, str(arguments.get("id") or ""), str(arguments.get("note") or ""), str(arguments.get("rating") or "helpful"), str(arguments.get("feedback_id") or "") or None)
    if name == "memory_supersede":
        if "memory" not in arguments:
            raise ValueError("memory_supersede requires memory")
        data_dir = data_root() / "incoming"
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="team-memory-replacement-", dir=data_dir, delete=False) as handle:
            json.dump(arguments.get("memory"), handle, ensure_ascii=False)
            path = Path(handle.name)
        try:
            return supersede_memory(str(arguments.get("id") or ""), str(path))
        finally:
            path.unlink(missing_ok=True)
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
        gate_failed = False
        root = None if args.command in {"init", "source", "doctor", "sync", "search", "get", "explain", "feedback", "promote", "publish", "team-activate", "team-export", "team-import", "team-evaluate", "team-compact", "context", "supersede", "ingest-session", "capture-turn", "knowledge", "memmy", "gui", "semantic", "memory", "memorycore"} else resolve_root(root_arg)
        if args.command in {"init", "source"} and root_arg:
            root = resolve_root(root_arg)
        if args.command == "doctor":
            value = doctor(root if root_arg else None, getattr(args, "source", None))
        elif args.command == "sync":
            value = sync_index(root if root_arg else None, args.deep, None if args.all else getattr(args, "source", None), args.local)
        elif args.command == "search":
            value = search(root if root_arg else None, args.query, args.limit, args.deep, getattr(args, "source", None), args.local, args.scope)
        elif args.command == "get":
            value = get_result(root if root_arg else None, args.result_id, expected_commit=args.commit, line_start=args.line_start, line_end=args.line_end)
        elif args.command == "explain":
            value = get_result(root if root_arg else None, args.result_id, explain=True, expected_commit=args.commit, line_start=args.line_start, line_end=args.line_end)
        elif args.command == "feedback":
            value = feedback(root, args.result_id, args.note, args.rating, args.feedback_id)
        elif args.command == "promote":
            value = promote(root, args.input)
        elif args.command == "publish":
            value = publish_memory(args.input, status=args.status)
        elif args.command == "team-activate":
            value = activate_memory(args.id, args.reviewer)
        elif args.command == "team-export":
            value = export_team_memory(args.output)
        elif args.command == "team-import":
            value = import_team_memory(args.input)
        elif args.command == "team-evaluate":
            from team_memory_eval import evaluate_team_memory

            value = evaluate_team_memory(Path(args.records).expanduser(), Path(args.queries).expanduser(), Path(args.qrels).expanduser(), limit=args.limit)
            if args.gate:
                metrics = value["metrics"]
                failures = []
                if metrics["precision_at_1"] < args.min_p1:
                    failures.append(f"precision_at_1 {metrics['precision_at_1']:.4f} < {args.min_p1:.4f}")
                if metrics["recall_at_5"] < args.min_recall:
                    failures.append(f"recall_at_5 {metrics['recall_at_5']:.4f} < {args.min_recall:.4f}")
                if metrics["negative_abstain_accuracy"] < args.min_negative:
                    failures.append(f"negative_abstain_accuracy {metrics['negative_abstain_accuracy']:.4f} < {args.min_negative:.4f}")
                if metrics["candidate_contamination"] > args.max_candidate_contamination:
                    failures.append(f"candidate_contamination {metrics['candidate_contamination']:.4f} > {args.max_candidate_contamination:.4f}")
                gate_failed = bool(failures)
                value["ok"] = not gate_failed
                value["gate"] = {"passed": not gate_failed, "failures": failures, "thresholds": {"min_p1": args.min_p1, "min_recall": args.min_recall, "min_negative": args.min_negative, "max_candidate_contamination": args.max_candidate_contamination}}
        elif args.command == "team-compact":
            from team_memory import team_memory_backend

            backend = team_memory_backend()
            value = backend.compact(keep=args.keep)
        elif args.command == "context":
            value = memory_context(root if root_arg else None, args.query, limit=args.limit, source_id=getattr(args, "source", None), repo=args.repo, issue=args.issue, branch=args.branch, agent=args.agent, local=args.local)
        elif args.command == "supersede":
            value = supersede_memory(args.id, args.input)
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
        elif args.command == "knowledge":
            from knowledge import KnowledgeClient

            client = KnowledgeClient()
            if args.action in {"configure", "install", "start", "stop"}:
                from knowledge_service import main as knowledge_service_main

                service_args = [args.action]
                if args.action == "configure":
                    for name in ("root", "endpoint", "port", "state_dir", "service_id", "team_id", "user_id", "agent_id", "wiki_id", "node_modules"):
                        value = getattr(args, name, None)
                        if value is not None:
                            service_args.extend([f"--{name.replace('_', '-')}", str(value)])
                return knowledge_service_main(service_args)
            if args.action == "status":
                value = {"schema_version": SCHEMA_VERSION, **client.health(), "wiki_id": client.wiki_id, "code_graph_id": client.code_graph_id}
            elif args.action == "create":
                if not args.name:
                    raise RuntimeError("knowledge create requires --name")
                value = {"schema_version": SCHEMA_VERSION, "ok": True, "operation": "create-wiki", "result": client.create_wiki(args.name), "canonical_repo_changed": False}
            elif args.action == "search":
                if not args.query:
                    raise RuntimeError("knowledge search requires --query")
                wiki_id = args.wiki_id or client.wiki_id
                if not wiki_id:
                    raise RuntimeError("knowledge search requires --wiki-id or a configured knowledge.wiki_id")
                raw = client.search(wiki_id, args.query, max(1, min(args.limit, 100)))
                # MemoryKnowledge pages do not inherently carry the Git
                # commit/line contract.  Keep them as candidates until a
                # caller validates the returned path against repository view.
                value = {"schema_version": SCHEMA_VERSION, "ok": True, "source": "tencentdb-memoryknowledge", "wiki_id": wiki_id, "verified": [], "candidates": raw.get("results", raw.get("items", [])), "abstain": not bool(raw.get("results", raw.get("items", []))), "retrieval_mode": "keyword-only", "citation_policy": "unverified-until-repository-readback", "canonical_repo_changed": False}
            elif args.action == "sync":
                if root is None and not args.source:
                    root = resolve_root(root_arg)
                wiki_id = args.wiki_id or client.wiki_id
                if not wiki_id:
                    raise RuntimeError("knowledge sync requires --wiki-id or a configured knowledge.wiki_id")
                specs = discover_sources(str(root) if root is not None else None, args.source)
                if len(specs) != 1:
                    raise RuntimeError("knowledge sync requires one source; pass --source <id>")
                view = prepare_view(specs[0], local=False)
                value = {
                    "schema_version": SCHEMA_VERSION,
                    "operation": "knowledge-sync",
                    "source": specs[0].id,
                    "repository": specs[0].repository,
                    "commit": view.commit,
                    "commit_type": view.commit_type,
                    "freshness": view.freshness,
                    **client.sync_source(view.path, wiki_id, deep=args.deep),
                }
        elif args.command == "memmy":
            client = memmy_memory_client()
            if args.action == "status":
                value = {"schema_version": SCHEMA_VERSION, **client.health(), "canonical_repo_changed": False}
            elif args.action == "configure":
                if not args.endpoint:
                    raise RuntimeError("memmy configure requires --endpoint")
                value = {"schema_version": SCHEMA_VERSION, **configure_memmy(args.endpoint, args.profile_id, args.user_id), "canonical_repo_changed": False}
            elif args.action == "search":
                if not args.query:
                    raise RuntimeError("memmy search requires --query")
                value = {
                    "schema_version": SCHEMA_VERSION,
                    "ok": True,
                    "provider": "memmy",
                    "retrieval_mode": client.health().get("embedding", {}).get("strategy", "keyword-only"),
                    "results": client.search(args.query, args.limit),
                    "canonical_repo_changed": False,
                }
        elif args.command == "gui":
            value = memmy_gui(args.open)
        elif args.command == "semantic":
            if args.action == "status":
                value = semantic_model_status()
            else:
                value = configure_semantic(model=args.model, enabled=not args.disable, allow_download=args.download)
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
        elif args.command == "memory":
            if args.action == "promote-l3" and not args.accept:
                raise RuntimeError("memory promote-l3 requires explicit --accept")
            client = native_memory_client()
            if args.action == "status":
                value = {"schema_version": SCHEMA_VERSION, **client.health(refresh=True, probe_layers=True), "external_mode": getattr(client, "backend", "") != "standalone-memory", "canonical_repo_changed": False}
            elif args.action == "project":
                value = {"schema_version": SCHEMA_VERSION, **project_memory_candidates()}
            else:
                value = promote_l3(args.candidate or "")
        elif args.command == "memorycore":
            if args.action == "promote-l3":
                if not args.accept:
                    raise RuntimeError("promote-l3 requires explicit --accept")
                value = promote_l3(args.candidate or "")
                print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
            if args.action == "status":
                # `memorycore status` is a public compatibility command.  It
                # must describe the actual default runtime and must not force
                # users to start the optional vendor service just to inspect
                # readiness.
                client = native_memory_client()
                value = {"schema_version": SCHEMA_VERSION, **client.health(refresh=True, probe_layers=True), "external_mode": getattr(client, "backend", "") != "standalone-memory", "canonical_repo_changed": False}
                print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
            from memorycore_service import main as memorycore_main

            service_args = [args.action]
            if args.action == "configure":
                for name in ("memorycore_root", "endpoint", "llm_base_url", "state_dir", "team_id", "agent_id", "user_id", "pipeline_mode"):
                    value = getattr(args, name, None)
                    if value:
                        service_args.extend([f"--{name.replace('_', '-')}", str(value)])
                if args.model:
                    service_args.extend(["--model", args.model])
            return memorycore_main(service_args)
        else:
            raise RuntimeError(f"unknown command: {args.command}")
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if gate_failed else 0
    except (OSError, RuntimeError, TypeError, AdapterError) as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
