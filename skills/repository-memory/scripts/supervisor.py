#!/usr/bin/env python3
"""Explicit, evidence-aware review for local/team memory candidates.

No model or endpoint is embedded here.  A host may configure a command as a
JSON argv array in ``REPOSITORY_MEMORY_SUPERVISOR_COMMAND`` or user config
under ``supervisor.command``.  Without that command this module reports
``hold`` and never pretends that a candidate was model-reviewed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discovery import data_root, read_config
from local_memory import SECRET_CONTENT
from standalone_memory import StandaloneMemoryClient
from team_memory import team_memory_store


def _command(explicit: list[str] | None = None) -> list[str] | None:
    if explicit:
        return explicit
    raw = os.environ.get("REPOSITORY_MEMORY_SUPERVISOR_COMMAND", "").strip()
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("REPOSITORY_MEMORY_SUPERVISOR_COMMAND must be a JSON argv array") from exc
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError("supervisor command must be a non-empty JSON argv array")
        return value
    configured = read_config().get("supervisor")
    if isinstance(configured, dict) and isinstance(configured.get("command"), list):
        value = configured["command"]
        if all(isinstance(item, str) and item for item in value):
            return value
    return None


def _receipt_path() -> Path:
    return data_root() / "supervisor" / "receipts.jsonl"


def _write_receipt(receipt: dict[str, Any]) -> None:
    path = _receipt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _checks(item: dict[str, Any]) -> dict[str, Any]:
    content = "\n".join(str(item.get(key) or "") for key in ("title", "summary", "content"))
    provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
    citations = provenance.get("citations") or provenance.get("commits")
    # Team records are experience provenance, never Git citations, so a
    # citations-only gate held every auto-captured candidate forever no matter
    # what the reviewer said -- measured on the live store, 284 of 289 records
    # were unactivatable by construction.  What the gate is really for is
    # traceability: the record must say where it came from.  A memory lineage
    # (which source memory, observed when) satisfies that; memory_timeline can
    # walk it.  A record with neither a citation nor a lineage still holds.
    lineage = bool(provenance.get("source_memory_id") and (provenance.get("observed_at") or provenance.get("run_id")))
    return {
        "secret_free": not bool(SECRET_CONTENT.search(content)),
        "content_present": len(str(item.get("content") or "").strip()) >= 20,
        "provenance_present": bool(citations) or lineage,
        "provenance_kind": "citation" if citations else ("memory-lineage" if lineage else "none"),
        "status_candidate": str(item.get("status") or "") == "candidate",
        "confidence": float(item.get("confidence") or 0.0),
    }


def _model_review(command: list[str] | None, item: dict[str, Any], checks: dict[str, Any]) -> dict[str, Any]:
    if not command:
        return {"decision": "hold", "reason": "supervisor command is not configured", "model_reviewed": False}
    payload = {"item": item, "checks": checks, "instructions": "Return JSON: decision accept|hold|reject, confidence 0..1, reason, unsupported_claims."}
    try:
        result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"decision": "hold", "reason": f"supervisor invocation failed: {exc}", "model_reviewed": False}
    if result.returncode != 0:
        return {"decision": "hold", "reason": (result.stderr or result.stdout or "supervisor returned non-zero")[:500], "model_reviewed": False}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"decision": "hold", "reason": "supervisor returned non-JSON output", "model_reviewed": False}
    if not isinstance(value, dict) or str(value.get("decision") or "hold") not in {"accept", "hold", "reject"}:
        return {"decision": "hold", "reason": "supervisor returned an invalid decision", "model_reviewed": False}
    return {**value, "decision": str(value.get("decision")), "model_reviewed": True}


def _review_item(item: dict[str, Any], command: list[str] | None, *, apply: bool, reviewer: str | None, lane: str, min_confidence: float = 0.7) -> dict[str, Any]:
    checks = _checks(item)
    model = _model_review(command, item, checks)
    accepted_by_checks = all((checks["secret_free"], checks["content_present"], checks["provenance_present"], checks["status_candidate"]))
    model_confidence = float(model.get("confidence") or 0.0)
    decision = str(model.get("decision") or "hold") if accepted_by_checks and model_confidence >= min_confidence else "hold"
    action = "none"
    activation = None
    effective_reviewer = reviewer or (f"supervisor:{model.get('model')}" if model.get("model") else None)
    if apply and decision == "accept" and model.get("model_reviewed") and effective_reviewer:
        if lane == "team":
            activation = team_memory_store().activate(str(item["id"]), reviewer=effective_reviewer)
            action = "activated" if activation.get("ok") else "failed"
        else:
            content = str(item.get("content") or "")
            if content and not re.search(r"^status:\s*accepted\s*$", content, re.MULTILINE):
                content = "status: accepted\n" + content
            activation = StandaloneMemoryClient().write_scenario(str(item.get("path") or item.get("session_id") or item["id"]), content)
            action = "accepted" if activation.get("status") == "accepted" else "failed"
    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lane": lane,
        "id": item.get("id"),
        "checks": checks,
        "model": {key: model.get(key) for key in ("model", "decision", "confidence", "reason", "unsupported_claims", "model_reviewed")},
        "decision": decision,
        "action": action,
        "reviewer": effective_reviewer,
        "readback": activation.get("memory", {}).get("citation") if isinstance(activation, dict) else None,
    }
    _write_receipt(receipt)
    return receipt


def supervise(*, lane: str = "all", apply: bool = False, limit: int = 100, reviewer: str | None = None, command: list[str] | None = None, min_confidence: float = 0.7) -> dict[str, Any]:
    """Review candidates; only ``apply`` can change user-level memory state."""

    if lane not in {"team", "memory", "all"}:
        raise ValueError("supervisor lane must be team, memory, or all")
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    command = _command(command)
    receipts: list[dict[str, Any]] = []
    if lane in {"team", "all"}:
        team = team_memory_store().search("", limit=max(1, limit), include_candidates=True)
        for item in team.get("candidates", [])[:limit]:
            receipts.append(_review_item(item, command, apply=apply, reviewer=reviewer, lane="team", min_confidence=min_confidence))
    if lane in {"memory", "all"}:
        memory = StandaloneMemoryClient()
        for item in memory.list_scenarios()[:limit]:
            if str(item.get("status")) == "candidate":
                receipts.append(_review_item(item, command, apply=apply, reviewer=reviewer, lane="memory", min_confidence=min_confidence))
    return {
        "schema_version": 1,
        "ok": True,
        "operation": "supervise",
        "applied": bool(apply),
        "model_configured": bool(command),
        "reviewed": len(receipts),
        "accepted": sum(item["action"] in {"activated", "accepted"} for item in receipts),
        "held": sum(item["decision"] == "hold" for item in receipts),
        "receipts": receipts,
        "receipt_path": str(_receipt_path()),
        "canonical_repo_changed": False,
    }
