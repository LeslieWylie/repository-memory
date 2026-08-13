#!/usr/bin/env python3
"""Evaluate citation-first retrieval with document-level qrels.

The evaluator is intentionally domain-neutral.  It measures the first
verified result, not candidates or stale material, so Precision@1 cannot be
inflated by a result that the runtime refused to cite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from core import search
from discovery import cache_root, git


def _records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError(f"expected a JSON array: {path}")
        return [item for item in value if isinstance(item, dict)]
    result: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError(f"line {number} is not an object: {path}")
        result.append(value)
    return result


def _qrels(qrels: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = defaultdict(dict)
    for item in qrels:
        query_id = str(item.get("query_id") or "")
        document_id = str(item.get("document_id") or "")
        try:
            relevance = int(item.get("relevance", 0))
        except (TypeError, ValueError):
            continue
        if query_id and document_id:
            grouped[query_id][document_id] = relevance
    return dict(grouped)


def audit_qrels(root: Path, queries: list[dict[str, Any]], qrels: list[dict[str, Any]], scope: str = "repository") -> dict[str, Any]:
    query_ids = [str(item.get("id") or "") for item in queries]
    known = set(query_ids)
    duplicate_queries = sorted({item for item in query_ids if item and query_ids.count(item) > 1})
    invalid_query_ids = sorted({item for item in query_ids if not item})
    duplicate_qrels: list[dict[str, str]] = []
    unknown: list[str] = []
    invalid: list[dict[str, str]] = []
    invalid_relevance: list[dict[str, Any]] = []
    unsupported_relevance: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in qrels:
        query_id = str(item.get("query_id") or "")
        document_id = str(item.get("document_id") or "")
        if not query_id or not document_id:
            invalid.append({"query_id": query_id, "document_id": document_id, "reason": "query_id and document_id are required"})
        if query_id not in known:
            unknown.append(query_id)
        try:
            relevance = int(item.get("relevance", 0))
        except (TypeError, ValueError):
            invalid_relevance.append({"query_id": query_id, "document_id": document_id, "relevance": item.get("relevance")})
            relevance = 0
        if relevance < 0:
            invalid_relevance.append({"query_id": query_id, "document_id": document_id, "relevance": relevance})
        if relevance not in {1, 2}:
            unsupported_relevance.append({"query_id": query_id, "document_id": document_id, "relevance": relevance})
        key = (query_id, document_id)
        if key in seen:
            duplicate_qrels.append({"query_id": query_id, "document_id": document_id})
        seen.add(key)
        source = str(item.get("source") or "")
        path = str(item.get("path") or "")
        expected = f"{source}:{path}" if source and path else ""
        if scope == "repository" and not expected:
            invalid.append({"query_id": query_id, "document_id": document_id, "reason": "repository qrels require source and path"})
        elif scope == "repository" and document_id != expected:
            invalid.append({"query_id": query_id, "document_id": document_id, "expected": expected})
        if scope != "memory":
            if not path:
                invalid.append({"query_id": query_id, "document_id": document_id, "reason": "repository qrels require path"})
            else:
                candidate = (root / path).resolve()
                root_resolved = root.resolve()
                if root_resolved not in candidate.parents and candidate != root_resolved:
                    invalid.append({"query_id": query_id, "document_id": document_id, "reason": f"path escapes root: {path}"})
                elif not candidate.is_file():
                    invalid.append({"query_id": query_id, "document_id": document_id, "reason": f"path does not exist: {path}"})
    grouped = _qrels(qrels)
    missing = sorted(
        str(item.get("id") or "")
        for item in queries
        if not item.get("expected_abstain") and not any(value > 0 for value in grouped.get(str(item.get("id") or ""), {}).values())
    )
    negatives_with_gold = sorted(
        str(item.get("id") or "")
        for item in queries
        if item.get("expected_abstain") and any(value > 0 for value in grouped.get(str(item.get("id") or ""), {}).values())
    )
    counts = [sum(value > 0 for value in grouped.get(query_id, {}).values()) for item in queries if not item.get("expected_abstain") for query_id in [str(item.get("id") or "")]]
    return {
        "ok": not (duplicate_queries or invalid_query_ids or duplicate_qrels or unknown or invalid or invalid_relevance or unsupported_relevance or missing or negatives_with_gold),
        "query_count": len(queries),
        "qrel_count": len(qrels),
        "duplicate_query_ids": duplicate_queries,
        "invalid_query_ids": invalid_query_ids,
        "duplicate_qrels": duplicate_qrels,
        "unknown_query_ids": sorted(set(unknown)),
        "invalid_document_ids_or_paths": invalid,
        "invalid_relevance": invalid_relevance,
        "unsupported_relevance": unsupported_relevance,
        "missing_positive_gold": missing,
        "negative_queries_with_gold": negatives_with_gold,
        "positive_gold_count_min": min(counts) if counts else 0,
        "positive_gold_count_max": max(counts) if counts else 0,
    }


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]


def _citation_valid(root: Path, item: dict[str, Any], expected_commit: str | None = None) -> bool:
    citation = item.get("citation") if isinstance(item.get("citation"), dict) else {}
    if citation.get("valid") is not True:
        return False
    if citation.get("source") == "memorycore":
        return bool(citation.get("memory_id") and citation.get("layer") in {"L0", "L1", "L2", "L3"})
    if expected_commit and citation.get("commit") != expected_commit:
        return False
    path = str(citation.get("path") or "")
    start = citation.get("line_start")
    end = citation.get("line_end")
    if not path or not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return False
    candidate = (root / path).resolve()
    root_resolved = root.resolve()
    if root_resolved not in candidate.parents and candidate != root_resolved:
        return False
    try:
        return candidate.is_file() and end <= len(candidate.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return False


def _revision_snapshot(root: Path, revision: str) -> tuple[Path, str]:
    commit = git(root, "rev-parse", revision)
    if not commit:
        raise RuntimeError(f"revision not found: {revision}")
    target = cache_root() / "eval-revisions" / f"{root.name}-{commit[:16]}"
    if (target / ".git").exists() and git(target, "rev-parse", "HEAD") == commit:
        return target, commit
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(["git", "clone", "--no-hardlinks", "--quiet", str(root), str(target)], text=True, capture_output=True, check=False, timeout=300)
    if clone.returncode:
        raise RuntimeError((clone.stderr or clone.stdout or "snapshot clone failed").strip())
    checkout = subprocess.run(["git", "-C", str(target), "checkout", "--detach", "--quiet", commit], text=True, capture_output=True, check=False, timeout=120)
    if checkout.returncode:
        raise RuntimeError((checkout.stderr or checkout.stdout or "snapshot checkout failed").strip())
    subprocess.run(["git", "-C", str(target), "remote", "set-url", "origin", ""], capture_output=True, check=False, timeout=30)
    return target, commit


def _select(result: dict[str, Any], scope: str, relevant: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, str]:
    if scope != "all":
        return list(result.get("verified", [])), list(result.get("candidates", [])), bool(result.get("abstain")), scope
    groups = result.get("groups") if isinstance(result.get("groups"), dict) else {}
    chosen = "repository"
    for name in ("repository", "memory"):
        group = groups.get(name) if isinstance(groups.get(name), dict) else {}
        if any(str(item.get("id") or "") in relevant for item in group.get("verified", [])):
            chosen = name
            break
    group = groups.get(chosen) if isinstance(groups.get(chosen), dict) else {}
    return list(group.get("verified", [])), list(group.get("candidates", [])), bool(group.get("abstain", True)), chosen


def evaluate_queries(root: Path, queries_path: Path, qrels_path: Path, *, limit: int = 5, deep: bool = False, local: bool = False, scope: str = "repository", revision: str | None = None) -> dict[str, Any]:
    evaluated_root = root
    evaluated_commit = git(root, "rev-parse", "HEAD")
    if revision:
        evaluated_root, evaluated_commit = _revision_snapshot(root, revision)
    queries = _records(queries_path)
    qrel_rows = _records(qrels_path)
    grouped = _qrels(qrel_rows)
    audit = audit_qrels(evaluated_root, queries, qrel_rows, scope)
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    previous_source_id = os.environ.get("REPOSITORY_MEMORY_SOURCE_ID")
    try:
        for query in queries:
            query_id = str(query.get("id") or "")
            relevant = {doc_id for doc_id, relevance in grouped.get(query_id, {}).items() if relevance > 0}
            primary_relevant = {doc_id for doc_id, relevance in grouped.get(query_id, {}).items() if relevance >= 2}
            source_id = str(query.get("source_scope") or root.name)
            if revision:
                os.environ["REPOSITORY_MEMORY_SOURCE_ID"] = source_id
            started = time.perf_counter()
            result = search(evaluated_root, str(query.get("query") or ""), limit=limit, deep=deep, source_id=source_id if revision else query.get("source_scope"), local=local or bool(revision), scope=scope)
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)
            hits, candidates, abstain, selected_scope = _select(result, scope, relevant)
            ids = [str(item.get("id") or "") for item in hits[:limit]]
            rank = next((number for number, item_id in enumerate(ids, 1) if item_id in relevant), None)
            primary_rank = next((number for number, item_id in enumerate(ids, 1) if item_id in primary_relevant), None)
            expected_abstain = bool(query.get("expected_abstain"))
            p1 = bool(not expected_abstain and ids and ids[0] in relevant)
            recall = len(set(ids) & relevant) / len(relevant) if relevant else (1.0 if expected_abstain and abstain and not hits else 0.0)
            row = {
                "id": query_id,
                "intent": str(query.get("intent") or query.get("category") or "unknown"),
                "query": query.get("query"),
                "expected_abstain": expected_abstain,
                "gold_ids": sorted(relevant),
                "primary_gold_ids": sorted(primary_relevant),
                "top1_id": ids[0] if ids else None,
                "top5_ids": ids,
                "precision_at_1": int(p1) if not expected_abstain else None,
                "primary_precision_at_1": int(bool(not expected_abstain and ids and ids[0] in primary_relevant)) if not expected_abstain else None,
                "mrr_at_5": 1 / rank if rank and not expected_abstain else None,
                "primary_mrr_at_5": 1 / primary_rank if primary_rank and not expected_abstain else None,
                "recall_at_5": recall if not expected_abstain else None,
                "abstain": abstain,
                "abstain_correct": bool(abstain and not hits) if expected_abstain else None,
                "verified_count": len(hits),
                "candidate_count": len(candidates),
                "citation_valid_count": sum(_citation_valid(evaluated_root, item, evaluated_commit) for item in hits),
                "citation_total": len(hits),
                "latency_ms": round(latency, 3),
                "mode": result.get("mode"),
                "freshness": result.get("freshness"),
                "diagnostics": result.get("diagnostics"),
                "selected_scope": selected_scope,
            }
            rows.append(row)
            buckets[row["intent"]].append(row)
    finally:
        if previous_source_id is None:
            os.environ.pop("REPOSITORY_MEMORY_SOURCE_ID", None)
        else:
            os.environ["REPOSITORY_MEMORY_SOURCE_ID"] = previous_source_id

    positive = [row for row in rows if not row["expected_abstain"]]
    negatives = [row for row in rows if row["expected_abstain"]]
    quality_by_id = {str(item.get("id") or ""): str(item.get("quality") or "focused") for item in queries}
    strict = [row for row in positive if quality_by_id.get(row["id"], "focused") not in {"ambiguous", "temporal-ambiguous"}]
    focused = [row for row in positive if quality_by_id.get(row["id"], "focused") == "focused"]
    verified = sum(row["verified_count"] for row in rows)
    candidates = sum(row["candidate_count"] for row in rows)
    citations = sum(row["citation_valid_count"] for row in rows)
    p1_hits = sum(int(row["precision_at_1"] or 0) for row in positive)
    primary_p1_hits = sum(int(row["primary_precision_at_1"] or 0) for row in positive)
    relevant_retrieved_at_5 = sum(len(set(row["top5_ids"]) & set(row["gold_ids"])) for row in positive)
    relevant_total = sum(len(row["gold_ids"]) for row in positive)

    def bucket(rows_for_intent: list[dict[str, Any]]) -> dict[str, Any]:
        positives = [row for row in rows_for_intent if not row["expected_abstain"]]
        return {
            "total": len(rows_for_intent),
            "precision_at_1": sum(row["precision_at_1"] or 0 for row in positives) / len(positives) if positives else 0.0,
            "primary_precision_at_1": sum(row["primary_precision_at_1"] or 0 for row in positives) / len(positives) if positives else 0.0,
            "mrr_at_5": sum(row["mrr_at_5"] or 0 for row in positives) / len(positives) if positives else 0.0,
            "primary_mrr_at_5": sum(row["primary_mrr_at_5"] or 0 for row in positives) / len(positives) if positives else 0.0,
            "recall_at_5": sum(row["recall_at_5"] or 0 for row in positives) / len(positives) if positives else 0.0,
            "p50_latency_ms": statistics.median([row["latency_ms"] for row in rows_for_intent]) if rows_for_intent else 0.0,
            "p95_latency_ms": _percentile([row["latency_ms"] for row in rows_for_intent], 0.95),
        }

    return {
        "schema_version": 1,
        "root": str(evaluated_root),
        "requested_root": str(root),
        "evaluated_commit": evaluated_commit,
        "revision": revision,
        "qrels_revision": git(qrels_path.parent, "rev-parse", "HEAD"),
        "queries_sha256": _hash(queries_path),
        "qrels_sha256": _hash(qrels_path),
        "queries": str(queries_path),
        "qrels": str(qrels_path),
        "limit": limit,
        "scope": scope,
        "retrieval_mode": "repository-citation-first" if scope == "repository" else "layered-memory" if scope == "memory" else "grouped",
        "qrels_audit": audit,
        "query_quality": {"positive_queries": len(positive), "negative_queries": len(negatives), "quality_counts": {quality: sum(str(item.get("quality") or "focused") == quality for item in queries) for quality in sorted({str(item.get("quality") or "focused") for item in queries})}},
        "precision_at_1": sum(row["precision_at_1"] or 0 for row in positive) / len(positive) if positive else 0.0,
        "precision_at_1_hits": p1_hits,
        "precision_at_1_total": len(positive),
        "primary_precision_at_1": sum(row["primary_precision_at_1"] or 0 for row in positive) / len(positive) if positive else 0.0,
        "primary_precision_at_1_hits": primary_p1_hits,
        "primary_precision_at_1_total": len(positive),
        "strict_precision_at_1": sum(row["precision_at_1"] or 0 for row in strict) / len(strict) if strict else 0.0,
        "focused_precision_at_1": sum(row["precision_at_1"] or 0 for row in focused) / len(focused) if focused else 0.0,
        "mrr_at_5": sum(row["mrr_at_5"] or 0 for row in positive) / len(positive) if positive else 0.0,
        "recall_at_5": sum(row["recall_at_5"] or 0 for row in positive) / len(positive) if positive else 0.0,
        "recall_at_5_relevant_retrieved": relevant_retrieved_at_5,
        "recall_at_5_relevant_total": relevant_total,
        "recall_at_5_micro": relevant_retrieved_at_5 / relevant_total if relevant_total else 0.0,
        "negative_abstain_accuracy": sum(bool(row["abstain_correct"]) for row in negatives) / len(negatives) if negatives else None,
        "citation_parseability": citations / verified if verified else 0.0,
        "candidate_contamination": candidates / (verified + candidates) if verified + candidates else 0.0,
        "p50_latency_ms": statistics.median(latencies) if latencies else 0.0,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "by_intent": {key: bucket(value) for key, value in sorted(buckets.items())},
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repository-memory evaluate")
    parser.add_argument("--root", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--qrels", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--scope", choices=("repository", "memory", "all"), default="repository")
    parser.add_argument("--revision")
    args = parser.parse_args(argv)
    report = evaluate_queries(Path(args.root).expanduser().resolve(), Path(args.queries).expanduser(), Path(args.qrels).expanduser(), limit=args.limit, deep=args.deep, local=args.local, scope=args.scope, revision=args.revision)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["qrels_audit"]["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
