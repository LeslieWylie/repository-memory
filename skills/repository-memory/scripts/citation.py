#!/usr/bin/env python3
"""Citation extraction and validation shared by every adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

RAW_PREFIX = "raw/sources/"


def normalize_path(value: Any) -> str | None:
    if not value:
        return None
    path = str(value).replace("\\", "/")
    return path.removeprefix(RAW_PREFIX)


def lines(value: dict[str, Any]) -> tuple[int | None, int | None]:
    locator = value.get("locator") or value.get("location") or {}
    if not isinstance(locator, dict):
        locator = {}
    start = locator.get("start_line", locator.get("line_start", value.get("line_start", value.get("start_line"))))
    end = locator.get("end_line", locator.get("line_end", value.get("line_end", value.get("end_line", start))))
    return (start if isinstance(start, int) else None, end if isinstance(end, int) else None)


def locate(root: Path, path: str | None, excerpt: str | None) -> tuple[int | None, int | None]:
    if not path:
        return None, None
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return None, None
    try:
        document = (root / relative).resolve()
        document.relative_to(root.resolve())
        content = document.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    file_lines = content.splitlines()
    excerpt_text = str(excerpt or "").strip("\n")
    if excerpt_text and excerpt_text in content:
        before, _separator, _after = content.partition(excerpt_text)
        start = before.count("\n") + 1
        return start, min(len(file_lines), start + max(1, len(excerpt_text.splitlines())) - 1)
    excerpt_lines = [line.strip() for line in excerpt_text.splitlines() if line.strip()]
    needle = next((line[:120] for line in sorted(excerpt_lines, key=len, reverse=True) if len(line) >= 12), "")
    if needle:
        for index, line in enumerate(file_lines):
            if needle in line:
                # A backend snippet may not be a contiguous source window;
                # anchor it to the first distinctive line with a bounded span.
                return index + 1, min(len(file_lines), index + max(1, min(12, len(excerpt_lines))))
    return (1, min(len(file_lines), 8)) if file_lines else (None, None)


def validate(root: Path, path: str | None, start: int | None, end: int | None, excerpt: str | None, commit: str | None, expected_commit: str | None) -> dict[str, Any]:
    if not path:
        return {"valid": False, "reason": "citation path missing"}
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return {"valid": False, "reason": "citation path must stay inside source root"}
    try:
        document = (root / relative).resolve()
        document.relative_to(root.resolve())
        file_lines = document.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {"valid": False, "reason": "citation path unavailable"}
    if not isinstance(start, int) or not isinstance(end, int) or not (1 <= start <= end <= max(1, len(file_lines))):
        return {"valid": False, "reason": "citation line range invalid"}
    evidence = str(excerpt or "").strip()
    if not evidence:
        return {"valid": False, "reason": "citation excerpt missing"}
    window = "\n".join(file_lines[start - 1:end])
    normalized_window = " ".join(window.split())
    evidence_lines = [" ".join(line.split()) for line in evidence.splitlines() if line.strip()]
    # Adapters may trim or reflow excerpts, so require one distinctive line in
    # the cited window instead of demanding byte equality.
    distinctive = [line for line in evidence_lines if len(line) >= 12]
    evidence_matches = any(line in normalized_window for line in distinctive)
    if not evidence_matches and evidence_lines:
        # Very short exact excerpts are valid anchors too.  The stronger
        # distinctive-line check is preferred for reflowed backend snippets,
        # but must not reject a real short source line.
        evidence_matches = " ".join(evidence_lines) in normalized_window
    if not evidence_matches:
        return {"valid": False, "reason": "citation excerpt does not match cited lines"}
    stale = bool(commit and expected_commit and commit != expected_commit)
    return {"valid": not stale, "stale": stale, "reason": "commit mismatch" if stale else None}


def validate_memory(citation: dict[str, Any], excerpt: str | None) -> dict[str, Any]:
    """Validate a native memory citation without pretending it is a Git file.

    MemoryCore records are not repository documents, so a path/line check is
    impossible.  They are verified through the native layer, stable record id,
    layer, and returned content instead.  Repository results still use the
    stricter ``validate`` path below.
    """
    source = str(citation.get("source") or "")
    layer = str(citation.get("layer") or "")
    memory_id = str(citation.get("memory_id") or "")
    evidence = str(citation.get("evidence") or excerpt or "").strip()
    if source not in {"memorycore", "local-memory", "memmy"}:
        return {"valid": False, "reason": "not a native memory citation"}
    if layer not in {"L0", "L1", "L2", "L3", "Skill"} or not memory_id:
        return {"valid": False, "reason": "native memory layer or id missing"}
    if not evidence:
        return {"valid": False, "reason": "native memory evidence missing"}
    return {"valid": True, "stale": False, "reason": None}


def evidence_status(item: dict[str, Any], citation: dict[str, Any]) -> str:
    status = str(item.get("evidence_status") or citation.get("evidence_status") or "").lower()
    if status in {"candidate", "pending", "inferred", "generated", "stale"}:
        return status
    if citation.get("generated") or item.get("generated"):
        return "generated"
    if citation.get("accepted") is True or item.get("accepted") is True:
        return "primary"
    return status or "secondary"


def result_is_verified(validation: dict[str, Any], status: str, commit: str | None, expected_commit: str | None) -> bool:
    if not validation.get("valid") or validation.get("stale"):
        return False
    if status in {"candidate", "pending", "inferred", "generated", "stale"}:
        return False
    return bool(commit or expected_commit)
