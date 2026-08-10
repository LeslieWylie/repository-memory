#!/usr/bin/env python3
"""Small public data objects shared by the repository-memory runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceSpec:
    id: str
    root: Path
    repository: str
    adapter: str | None = None
    remote: str | None = None
    branch: str | None = None
    profile: str | None = None


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
