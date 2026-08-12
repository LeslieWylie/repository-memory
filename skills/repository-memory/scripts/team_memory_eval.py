#!/usr/bin/env python3
"""Evaluate shared Team Memory retrieval without touching the user database."""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from team_memory import SQLiteTeamMemoryBackend


def _records(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"line {number} is not an object: {path}")
        values.append(value)
    return values


def evaluate_team_memory(records_path: Path, queries_path: Path, qrels_path: Path, *, limit: int = 5) -> dict[str, Any]:
    records = _records(records_path)
    queries = _records(queries_path)
    qrels = _records(qrels_path)
    gold: dict[str, set[str]] = {}
    for item in qrels:
        if int(item.get("relevance", 0)) > 0:
            gold.setdefault(str(item.get("query_id") or ""), set()).add(str(item.get("memory_id") or ""))

    with tempfile.TemporaryDirectory(prefix="repository-memory-team-eval-") as directory:
        store = SQLiteTeamMemoryBackend(Path(directory) / "team.sqlite3")
        for record in records:
            store.publish(record, default_status=str(record.get("status") or "candidate"))
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for query in queries:
            query_id = str(query.get("id") or "")
            started = time.perf_counter()
            result = store.search(str(query.get("query") or ""), limit=limit)
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            active = result.get("active", [])[:limit]
            candidates = result.get("candidates", [])[:limit]
            ids = [str(item.get("id") or "") for item in active]
            relevant = gold.get(query_id, set())
            expected_abstain = bool(query.get("expected_abstain"))
            rank = next((position for position, item_id in enumerate(ids, 1) if item_id in relevant), None)
            rows.append({
                "id": query_id,
                "query": query.get("query"),
                "gold_ids": sorted(relevant),
                "top1_id": ids[0] if ids else None,
                "top5_ids": ids,
                "precision_at_1": int(bool(ids and ids[0] in relevant)) if not expected_abstain else None,
                "mrr_at_5": 1 / rank if rank and not expected_abstain else None,
                "recall_at_5": len(set(ids) & relevant) / len(relevant) if relevant else None,
                "expected_abstain": expected_abstain,
                "abstain": bool(result.get("abstain")),
                "abstain_correct": bool(result.get("abstain") and not active) if expected_abstain else None,
                "active_count": len(active),
                "candidate_count": len(candidates),
                "candidate_contamination": sum(item.get("status") == "candidate" for item in active),
                "latency_ms": round(latency, 3),
            })

    positives = [row for row in rows if not row["expected_abstain"]]
    negatives = [row for row in rows if row["expected_abstain"]]
    return {
        "schema_version": 1,
        "ok": True,
        "records": len(records),
        "queries": len(rows),
        "metrics": {
            "precision_at_1": sum(row["precision_at_1"] or 0 for row in positives) / len(positives) if positives else 0.0,
            "mrr_at_5": statistics.mean(row["mrr_at_5"] or 0 for row in positives) if positives else 0.0,
            "recall_at_5": statistics.mean(row["recall_at_5"] or 0 for row in positives) if positives else 0.0,
            "negative_abstain_accuracy": sum(bool(row["abstain_correct"]) for row in negatives) / len(negatives) if negatives else 1.0,
            "candidate_contamination": sum(row["candidate_contamination"] for row in rows) / len(rows) if rows else 0.0,
            "candidate_exposure_rate": sum(bool(row["candidate_count"]) for row in rows) / len(rows) if rows else 0.0,
            "p50_latency_ms": statistics.median(latencies) if latencies else 0.0,
            "p95_latency_ms": sorted(latencies)[min(len(latencies) - 1, max(0, int((len(latencies) - 1) * 0.95)))] if latencies else 0.0,
        },
        "rows": rows,
        "retrieval_mode": "lexical",
        "semantic_available": False,
        "canonical_repo_changed": False,
    }
