#!/usr/bin/env python3
"""Small public data objects shared by the repository-memory runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MEMORY_LAYERS = ("L0", "L1", "L2", "L3")
MEMORY_CAPABILITIES = {"supported", "unsupported", "unknown"}
MEMORY_API_STATES = {"ready", "unreachable", "not_configured", "unsupported", "unknown"}
MEMORY_POPULATION_STATES = {"empty", "present", "unknown"}
MEMORY_READBACK_STATES = {"verified", "pending", "unknown"}


def memory_layer_state(
    capability: str,
    api_status: str,
    population: str,
    readback: str,
    **details: Any,
) -> dict[str, Any]:
    """Build the provider-neutral per-layer doctor contract.

    Capability, API readiness, stored-data population, and read-back evidence
    are deliberately independent.  In particular, a supported or reachable
    layer is not evidence that it contains any records.
    """

    if capability not in MEMORY_CAPABILITIES:
        raise ValueError(f"invalid memory capability: {capability}")
    if api_status not in MEMORY_API_STATES:
        raise ValueError(f"invalid memory API status: {api_status}")
    if population not in MEMORY_POPULATION_STATES:
        raise ValueError(f"invalid memory population: {population}")
    if readback not in MEMORY_READBACK_STATES:
        raise ValueError(f"invalid memory readback: {readback}")
    return {
        "capability": capability,
        "api_status": api_status,
        "population": population,
        "readback": readback,
        **details,
    }


@dataclass(frozen=True)
class SourceSpec:
    id: str
    root: Path
    repository: str
    adapter: str | None = None
    remote: str | None = None
    branch: str | None = None
    profile: str | None = None
    # A local-only source is an explicit, user-managed snapshot.  It must not
    # be treated as a failed remote fetch merely because it has no origin.
    local_only: bool = False


@dataclass
class SourceView:
    spec: SourceSpec
    path: Path
    commit: str | None
    branch: str | None
    commit_type: str
    dirty: bool
    remote_url: str | None = None
    remote_commit: str | None = None
    fetch_ok: bool | None = None
    fetch_error: str | None = None
    snapshot: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def freshness(self) -> dict[str, Any]:
        if self.dirty:
            state = "dirty"
        elif self.fetch_error:
            state = "fallback"
        else:
            state = "fresh" if self.commit else "unknown"
        return {
            "state": state,
            "commit": self.commit,
            "commit_type": self.commit_type,
            "remote_commit": self.remote_commit,
            "dirty": self.dirty,
            "fetch_ok": self.fetch_ok,
            "fetch_error": self.fetch_error,
            "snapshot": self.snapshot,
        }
