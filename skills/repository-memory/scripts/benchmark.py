#!/usr/bin/env python3
"""Reproducible benchmark entry point without bundling external datasets."""

from __future__ import annotations

import json
import os
import site
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from evaluate import evaluate_queries


SUPPORTED_SUITES = {"public", "agentmemories", "locomo", "longmemeval", "rlvr"}


def _local_public_root() -> Path | None:
    """Find the checked-out public core when the command is run at its root."""

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "README.md").is_file() and (candidate / "skills" / "repository-memory").is_dir():
            return candidate
    configured = os.environ.get("REPOSITORY_MEMORY_ROOT")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if (candidate / "README.md").is_file():
            return candidate
    return None


def _user_data_public() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "share"
    return base / "repository-memory" / "eval" / "public"


def _paths(suite: str, data: Path | None, queries: Path | None, qrels: Path | None) -> tuple[Path, Path, Path | None]:
    if queries and qrels:
        return queries, qrels, data
    if data is None:
        if suite == "public":
            candidates = [
                Path(__file__).resolve().parents[3] / "eval" / "public",
                _user_data_public(),
                Path(sys.prefix) / "share" / "repository-memory" / "eval" / "public",
                Path(site.getuserbase()) / "share" / "repository-memory" / "eval" / "public",
            ]
            data = next((item for item in candidates if item.exists()), candidates[0])
        else:
            raise ValueError(f"{suite} requires --data or --queries/--qrels")
    if data.is_file():
        value = json.loads(data.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("benchmark manifest must be a JSON object")
        base = data.parent
        queries = base / str(value.get("queries"))
        qrels = base / str(value.get("qrels"))
        root = base / str(value["root"]) if value.get("root") else None
        return queries, qrels, root
    if data.is_dir():
        queries = data / "queries.jsonl"
        qrels = data / "qrels.jsonl"
        return queries, qrels, None
    raise ValueError(f"benchmark data path does not exist: {data}")


@contextmanager
def _semantic_override(model: str | None, download: bool = False):
    """Select an optional encoder for one benchmark without changing config."""

    if not model:
        yield
        return
    names = (
        "REPOSITORY_MEMORY_SEMANTIC_PROVIDER",
        "REPOSITORY_MEMORY_SEMANTIC_MODEL",
        "REPOSITORY_MEMORY_SEMANTIC_ENABLED",
        "REPOSITORY_MEMORY_SEMANTIC_ALLOW_DOWNLOAD",
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update({
        "REPOSITORY_MEMORY_SEMANTIC_PROVIDER": "huggingface",
        "REPOSITORY_MEMORY_SEMANTIC_MODEL": model,
        "REPOSITORY_MEMORY_SEMANTIC_ENABLED": "1",
        "REPOSITORY_MEMORY_SEMANTIC_ALLOW_DOWNLOAD": "1" if download else "0",
    })
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_benchmark(*, suite: str, root: Path | None, data: Path | None = None, queries: Path | None = None, qrels: Path | None = None, limit: int = 5, revision: str | None = None, semantic_model: str | None = None, semantic_download: bool = False) -> dict[str, Any]:
    if suite not in SUPPORTED_SUITES:
        raise ValueError(f"unsupported benchmark suite: {suite}")
    query_path, qrel_path, manifest_root = _paths(suite, data, queries, qrels)
    evaluated_root = root or manifest_root
    if evaluated_root is None and suite == "public":
        evaluated_root = _local_public_root()
    if evaluated_root is None:
        return {"schema_version": 1, "ok": False, "suite": suite, "status": "missing_root", "error": "run from the public repository root, pass --root, or provide root in the benchmark manifest", "canonical_repo_changed": False}
    if not query_path.is_file() or not qrel_path.is_file():
        return {"schema_version": 1, "ok": False, "suite": suite, "status": "unsupported_data_format", "error": "expected queries.jsonl and qrels.jsonl; no external data was downloaded", "queries": str(query_path), "qrels": str(qrel_path), "canonical_repo_changed": False}
    with _semantic_override(semantic_model, semantic_download):
        report = evaluate_queries(evaluated_root.resolve(), query_path.resolve(), qrel_path.resolve(), limit=limit, revision=revision)
    report["semantic_requested"] = semantic_model
    report["semantic_download_requested"] = bool(semantic_download)
    modes: set[str] = set()
    neural_available: set[bool] = set()
    for row in report.get("rows", []):
        diagnostics = row.get("diagnostics") if isinstance(row, dict) else None
        adapters = diagnostics.get("adapters", []) if isinstance(diagnostics, dict) else []
        for adapter in adapters:
            semantic = adapter.get("semantic", {}) if isinstance(adapter, dict) else {}
            if isinstance(semantic, dict):
                if semantic.get("strategy"):
                    modes.add(str(semantic["strategy"]))
                if "native_neural_model" in semantic:
                    neural_available.add(bool(semantic.get("available") and semantic.get("native_neural_model")))
    report["effective_retrieval_modes"] = sorted(modes) or [str(report.get("retrieval_mode") or "unknown")]
    report["neural_model_available"] = True in neural_available
    return {"schema_version": 1, "ok": bool(report.get("qrels_audit", {}).get("ok")), "suite": suite, "status": "completed", "report": report, "canonical_repo_changed": False}
