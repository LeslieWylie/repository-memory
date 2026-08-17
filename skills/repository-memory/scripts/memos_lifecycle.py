"""Small, dependency-free pieces ported from MemOS Local's lifecycle ideas.

This module intentionally keeps the repository-memory runtime independent from
the MemOS TypeScript package.  The implementation follows the useful contracts
from MemOS Local: episode/turn boundaries, feedback-weighted values, and an
L2 candidate pool that requires evidence from multiple episodes.  It does not
copy provider, daemon, or agent-specific code.

MemOS Local is Apache-2.0; see vendor attribution in ``docs/memory-providers``.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Iterable


_TOKEN_RE = re.compile(r"[\w一-龥./:-]{2,}", re.UNICODE)
_ERROR_RE = re.compile(r"错误|失败|报错|异常|阻塞|timeout|error|failed|blocked", re.I)
_REVISION_RE = re.compile(r"不对|错了|重做|改一下|重新|wrong|incorrect|redo", re.I)
_NEW_TASK_RE = re.compile(r"换个|另一个|新任务|new task|new topic|moving on", re.I)


def tokens(text: str) -> list[str]:
    return list(dict.fromkeys(t.casefold() for t in _TOKEN_RE.findall(text or "")))


def signature(text: str) -> str:
    """Return a stable, conservative pattern bucket for candidate pooling."""

    values = tokens(text)
    tags = []
    if _ERROR_RE.search(text or ""):
        tags.append("failure")
    if _REVISION_RE.search(text or ""):
        tags.append("revision")
    if _NEW_TASK_RE.search(text or ""):
        tags.append("new-task")
    # Keep a few discriminative terms, not the whole conversation.
    values = sorted(values[:8])
    return "|".join([*tags, *values])[:220] or "empty"


def signature_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def classify_turn(previous: str | None, current: str, gap_seconds: float | None = None) -> dict[str, Any]:
    """Classify a turn without requiring an LLM.

    Strong correction and explicit new-task phrases win.  Ambiguous turns stay
    ``follow_up`` so the runtime never silently discards context.
    """

    text = (current or "").strip()
    gap = float(gap_seconds or 0)
    if not previous:
        return {"relation": "new_task", "confidence": 0.75, "rule": "no_previous_context"}
    if _REVISION_RE.search(text):
        return {"relation": "revision", "confidence": 0.85, "rule": "correction_phrase"}
    if _NEW_TASK_RE.search(text):
        return {"relation": "new_task", "confidence": 0.85, "rule": "new_task_phrase"}
    if gap > 2 * 60 * 60:
        return {"relation": "new_task", "confidence": 0.9, "rule": "idle_timeout"}
    return {"relation": "follow_up", "confidence": 0.5, "rule": "safe_default"}


def backpropagate(traces: Iterable[dict[str, Any]], reward: float, *, gamma: float = 0.9, half_life_days: float = 30.0, now: float | None = None) -> list[dict[str, Any]]:
    """Apply MemOS-style terminal reward propagation and time decay."""

    rows = list(traces)
    current = max(-1.0, min(1.0, float(reward)))
    gamma = max(0.0, min(1.0, float(gamma)))
    now = float(now if now is not None else time.time())
    half_life = max(1.0, float(half_life_days))
    output: list[dict[str, Any]] = []
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        alpha = max(0.0, min(1.0, float(row.get("alpha", 0.3))))
        value = current if index == len(rows) - 1 else alpha * current + (1 - alpha) * gamma * current
        timestamp = float(row.get("timestamp_epoch") or now)
        age_days = max(0.0, (now - timestamp) / 86400.0)
        priority = max(value, 0.0) * (0.5 ** (age_days / half_life))
        output.append({"id": row.get("id"), "value": value, "priority": priority, "alpha": alpha})
        current = value
    output.reverse()
    return output


def feedback_value(rating: str) -> float:
    return {
        "helpful": 0.15,
        "up": 0.15,
        "correct": 0.15,
        "wrong": -0.35,
        "stale": -0.2,
        "not_helpful": -0.1,
        "down": -0.1,
    }.get((rating or "").casefold(), 0.0)


def ready_buckets(records: Iterable[dict[str, Any]], *, min_distinct_episodes: int = 2) -> list[dict[str, Any]]:
    """Group L1 records into evidence-backed L2 candidate buckets."""

    buckets: dict[str, dict[str, Any]] = {}
    for row in records:
        content = str(row.get("content") or "")
        key = signature(content)
        bucket = buckets.setdefault(key, {"signature": key, "records": [], "episodes": set()})
        bucket["records"].append(row)
        bucket["episodes"].add(str(row.get("episode_id") or row.get("session_id") or ""))
    ready = []
    for bucket in buckets.values():
        episodes = sorted(item for item in bucket["episodes"] if item)
        if len(episodes) >= min_distinct_episodes:
            bucket["episodes"] = episodes
            bucket["record_ids"] = [str(row.get("id")) for row in bucket["records"]]
            ready.append(bucket)
    ready.sort(key=lambda item: (-len(item["record_ids"]), item["signature"]))
    return ready


def policy_candidate(bucket: dict[str, Any]) -> dict[str, Any]:
    records = bucket["records"]
    excerpts = [str(row.get("content") or "")[:600] for row in records[:5]]
    return {
        "status": "candidate",
        "layer": "L2",
        "kind": "policy",
        "trigger": bucket["signature"],
        "procedure": ["Inspect the cited evidence", "Apply only within the observed scope", "Verify the result"],
        "verification": "Confirm the outcome against the linked evidence before reuse.",
        "boundary": "Do not generalize beyond the linked episodes or treat candidate status as fact.",
        "support": {"episode_count": len(bucket["episodes"]), "record_ids": bucket["record_ids"]},
        "evidence": excerpts,
    }
