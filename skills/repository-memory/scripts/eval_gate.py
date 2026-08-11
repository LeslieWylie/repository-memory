#!/usr/bin/env python3
"""Small deterministic regression gate for the public citation benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluate import evaluate_queries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repository-memory eval-gate")
    parser.add_argument("--root", required=True)
    parser.add_argument("--queries", required=True)
    parser.add_argument("--qrels", required=True)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--revision", help="Evaluate a detached revision snapshot instead of a dirty worktree.")
    parser.add_argument("--min-strict-p1", type=float, default=0.80)
    parser.add_argument("--min-recall-at-5", type=float, default=0.80)
    args = parser.parse_args(argv)
    report = evaluate_queries(
        Path(args.root).expanduser().resolve(),
        Path(args.queries).expanduser().resolve(),
        Path(args.qrels).expanduser().resolve(),
        local=args.local,
        scope="repository",
        revision=args.revision,
    )
    checks = {
        "qrels_audit": bool(report["qrels_audit"]["ok"]),
        "strict_precision_at_1": report["strict_precision_at_1"] >= args.min_strict_p1,
        "recall_at_5": report["recall_at_5"] >= args.min_recall_at_5,
        "citation_parseability": report["citation_parseability"] == 1.0,
        "negative_abstain_accuracy": report["negative_abstain_accuracy"] in (None, 1.0),
    }
    output = {
        "schema_version": 1,
        "ok": all(checks.values()),
        "checks": checks,
        "metrics": {
            key: report[key]
            for key in (
                "evaluated_commit",
                "strict_precision_at_1",
                "precision_at_1",
                "mrr_at_5",
                "recall_at_5",
                "recall_at_5_micro",
                "citation_parseability",
                "negative_abstain_accuracy",
                "p50_latency_ms",
                "p95_latency_ms",
            )
        },
        "qrels_audit": report["qrels_audit"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
