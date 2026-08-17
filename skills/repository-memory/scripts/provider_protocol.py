#!/usr/bin/env python3
"""Provider-neutral JSON contract for optional Repository Memory backends.

The standalone SQLite runtime is the default.  This module only defines the
small boundary an optional provider must satisfy; it intentionally contains no
provider SDK, network client, model name, or credential handling.
"""

from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = 1
OPERATIONS = ("doctor", "sync", "search", "get", "timeline", "feedback", "promote")


def capabilities(*operations: str) -> list[str]:
    """Return stable, deduplicated operation names for a provider manifest."""

    allowed = set(OPERATIONS)
    value = {str(item).strip() for item in operations if str(item).strip() in allowed}
    return [item for item in OPERATIONS if item in value]


def normalize_response(value: Any, *, operation: str, provider: str) -> dict[str, Any]:
    """Wrap a provider response without changing its result payload."""

    if operation not in OPERATIONS:
        raise ValueError(f"unsupported provider operation: {operation}")
    if isinstance(value, dict):
        result = dict(value)
    else:
        result = {"result": value}
    result.setdefault("schema_version", PROTOCOL_VERSION)
    result.setdefault("provider", provider)
    result.setdefault("operation", operation)
    result.setdefault("canonical_repo_changed", False)
    return result


def manifest(provider: str, operations: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Build a serializable provider manifest for doctor/config tooling."""

    return {
        "protocol": "repository-memory-provider",
        "protocol_version": PROTOCOL_VERSION,
        "provider": str(provider),
        "capabilities": capabilities(*operations),
        "default": False,
        "citation_source_of_truth": "git-repository",
    }
