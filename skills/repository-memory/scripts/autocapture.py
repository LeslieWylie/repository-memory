#!/usr/bin/env python3
"""Safe post-turn normalization and candidate extraction.

This module contains policy for the OpenClaw adapter, not a second retrieval
backend.  It accepts generic role/content messages, removes tool and secret
material, and produces a bounded payload for the shared runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----", re.I | re.S),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b", re.I),
)
DROP_ROLES = {"tool", "toolResult", "function", "system", "developer"}
DURABLE_HINTS = re.compile(
    r"决定|完成|修复|实现|计划|偏好|记住|配置|提交|合并|阻塞|结论|以后|迁移|上线|规则|decision|done|fixed|implemented|plan|prefer|remember|config|commit|merge|blocked|conclusion|policy",
    re.I,
)
INJECTED_CONTEXT = re.compile(
    r"<(?:relevant-memories|user-persona|relevant-scenes|scene-navigation|memory-tools-guide|"
    r"current_task_context|history_task_context)[^>]*>[\s\S]*?</(?:relevant-memories|user-persona|"
    r"relevant-scenes|scene-navigation|memory-tools-guide|current_task_context|history_task_context)>",
    re.I,
)


def sanitize_text(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    # The upstream OpenClaw client strips injected recall blocks before L0
    # capture.  Without this, a prompt-injected memory becomes the next
    # conversation's apparent user/assistant evidence and creates a feedback
    # loop in L1 extraction.
    text = INJECTED_CONTEXT.sub("", text)
    text = re.sub(r"\[\[reply_to[^\]]*\]\]\s*", "", text, flags=re.I)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    text = text.replace("\x00", " ").strip()
    return text[:max_chars]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(parts)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    return ""


def _timestamp_value(value: Any) -> float | None:
    """Normalize common OpenClaw millisecond/ISO cursors."""

    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            number = float(raw)
            return number / 1000 if number > 10_000_000_000 else number
        except ValueError:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def _message_timestamp(raw: dict[str, Any]) -> float | None:
    for key in ("timestamp", "ts", "created_at", "createdAt", "time"):
        value = _timestamp_value(raw.get(key))
        if value is not None:
            return value
    return None


def _capture_messages(payload: dict[str, Any]) -> list[Any]:
    """Apply upstream-style turn boundaries before role/content cleanup.

    OpenClaw can send the whole session to ``agent_end``.  A position cursor
    is preferred when the host exposes it; an optional timestamp cursor then
    removes older stamped messages.  If a host omits either cursor we retain
    the previous bounded behavior rather than guessing and dropping data.
    """

    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return []
    start_value = payload.get("original_user_message_count")
    try:
        start = max(0, int(start_value)) if start_value is not None else None
    except (TypeError, ValueError):
        start = None
    selected = raw_messages[start:] if start is not None and start <= len(raw_messages) else raw_messages
    cursor = _timestamp_value(payload.get("after_timestamp"))
    if cursor is not None:
        stamped = [_message_timestamp(item) for item in selected if isinstance(item, dict)]
        if any(value is not None for value in stamped):
            selected = [
                item for item in selected
                if not isinstance(item, dict)
                or _message_timestamp(item) is None
                or (_message_timestamp(item) or 0) > cursor
            ]
    return selected


def normalize_turn(payload: dict[str, Any], *, max_messages: int = 24, max_message_chars: int = 12000) -> dict[str, Any]:
    """Return a bounded, provider-neutral session accepted by MemoryCore."""

    raw_messages = _capture_messages(payload)
    if not isinstance(raw_messages, list):
        raise ValueError("post-turn payload requires a messages list")
    original_user_text = sanitize_text(payload.get("original_user_text"), max_message_chars)
    last_user_index = max((index for index, raw in enumerate(raw_messages) if isinstance(raw, dict) and raw.get("role") == "user"), default=-1)
    messages: list[dict[str, str]] = []
    for index, raw in enumerate(raw_messages[-max_messages:], start=max(0, len(raw_messages) - max_messages)):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip()
        if role in DROP_ROLES:
            continue
        if role not in {"user", "assistant"}:
            continue
        source_content = original_user_text if original_user_text and index == last_user_index else _content_text(raw.get("content"))
        content = sanitize_text(source_content, max_message_chars)
        if role == "assistant":
            # Match the upstream capture boundary: code is useful for a
            # coding answer but too noisy to promote into durable memory.
            content = re.sub(r"```[^\n]*\n[\s\S]*?```", "", content)
            content = re.sub(r"\n{3,}", "\n\n", content).strip()
        if content:
            messages.append({"role": role, "content": content})
    if not any(message["role"] == "user" for message in messages):
        raise ValueError("post-turn payload has no user message")
    if not any(message["role"] == "assistant" for message in messages):
        raise ValueError("post-turn payload has no assistant message")
    session_id = sanitize_text(payload.get("session_id") or payload.get("sessionKey") or "openclaw-session", 240)
    run_id = sanitize_text(payload.get("run_id") or payload.get("turn_id") or "", 240)
    return {
        "session_id": session_id,
        "run_id": run_id,
        "agent_id": sanitize_text(payload.get("agent_id") or "", 120),
        "workspace": sanitize_text(payload.get("workspace") or "", 500),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
    }


def should_create_candidate(turn: dict[str, Any], min_answer_chars: int = 80) -> bool:
    answers = [message["content"] for message in turn["messages"] if message["role"] == "assistant"]
    answer = answers[-1] if answers else ""
    return len(answer) >= min_answer_chars or bool(DURABLE_HINTS.search(answer))


def candidate_identity(turn: dict[str, Any]) -> str:
    basis = "\n".join(f"{item['role']}:{item['content']}" for item in turn["messages"])
    return hashlib.sha256((turn.get("run_id") or basis).encode("utf-8")).hexdigest()[:24]


def candidate_path(turn: dict[str, Any]) -> str:
    stamp = str(turn.get("captured_at") or datetime.now(timezone.utc).isoformat())[:10]
    return f"autocapture/candidates/{stamp}/{candidate_identity(turn)}.md"


def candidate_store_path(data_root: Path, path: str, identity: dict[str, str] | None = None) -> Path:
    """Map a generated candidate id into the user-level derived store."""

    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not str(relative).startswith("autocapture/candidates/"):
        raise ValueError("invalid autocapture candidate path")
    if identity:
        identity_text = json.dumps({key: identity.get(key, "") for key in ("team_id", "agent_id", "user_id")}, sort_keys=True)
        namespace = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:20]
        return data_root / "autocapture" / "identities" / namespace / relative.relative_to("autocapture")
    return data_root / relative


def candidate_markdown(turn: dict[str, Any], l0: dict[str, Any], l1: dict[str, Any]) -> str:
    user = next((item["content"] for item in reversed(turn["messages"]) if item["role"] == "user"), "")
    answer = next((item["content"] for item in reversed(turn["messages"]) if item["role"] == "assistant"), "")
    metadata = {
        "status": "candidate",
        "layer": "L2",
        "source": "openclaw-agent-end",
        "captured_at": turn.get("captured_at"),
        "session_id": turn.get("session_id"),
        "run_id": turn.get("run_id") or None,
        "l0_verified": bool(l0.get("l0_verified")),
        "l0_ids": l0.get("record_ids") or [],
        "l1_status": l1.get("status"),
        "evidence_status": "pending",
    }
    lines = ["---"]
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, list):
            lines.append(f"{key}: {', '.join(str(item) for item in value)}")
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "", "# Candidate memory", "", "## User", "", user, "", "## Assistant", "", answer, ""])
    return "\n".join(lines)
