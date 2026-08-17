#!/usr/bin/env python3
"""Reproducible benchmark entry point without bundling external datasets."""

from __future__ import annotations

import json
import site
import sys
from pathlib import Path
from typing import Any

from evaluate import evaluate_queries


SUPPORTED_SUITES = {"public", "agentmemories", "locomo", "longmemeval", "rlvr"}


def _paths(suite: str, data: Path | None, queries: Path | None, qrels: Path | None) -> tuple[Path, Path, Path | None]:
    if queries and qrels:
        return queries, qrels, data
    if data is None:
        if suite == "public":
            candidates = [
                Path(__file__).resolve().parents[3] / "eval" / "public",
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


def run_benchmark(*, suite: str, root: Path | None, data: Path | None = None, queries: Path | None = None, qrels: Path | None = None, limit: int = 5, revision: str | None = None) -> dict[str, Any]:
    if suite not in SUPPORTED_SUITES:
        raise ValueError(f"unsupported benchmark suite: {suite}")
    query_path, qrel_path, manifest_root = _paths(suite, data, queries, qrels)
    evaluated_root = root or manifest_root
    if evaluated_root is None:
        return {"schema_version": 1, "ok": False, "suite": suite, "status": "missing_root", "error": "pass --root or provide root in the benchmark manifest", "canonical_repo_changed": False}
    if not query_path.is_file() or not qrel_path.is_file():
        return {"schema_version": 1, "ok": False, "suite": suite, "status": "unsupported_data_format", "error": "expected queries.jsonl and qrels.jsonl; no external data was downloaded", "queries": str(query_path), "qrels": str(qrel_path), "canonical_repo_changed": False}
    report = evaluate_queries(evaluated_root.resolve(), query_path.resolve(), qrel_path.resolve(), limit=limit, revision=revision)
    return {"schema_version": 1, "ok": bool(report.get("qrels_audit", {}).get("ok")), "suite": suite, "status": "completed", "report": report, "canonical_repo_changed": False}
