#!/usr/bin/env python3
"""Small persistent lexical index used when no external adapter is available.

The index is derived state in the user cache.  Canonical files remain the
source of truth and every record is keyed by the source revision, so a changed
directory cannot silently reuse an old document snapshot.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from discovery import cache_root, fingerprint
from fallback import _read_document, paths

from models import SourceView

SCHEMA_VERSION = 4


def _revision_key(view: SourceView, deep: bool) -> str:
    revision = str(view.commit or "unknown")
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in revision)
    return f"{safe or 'unknown'}-{'deep' if deep else 'default'}"


def index_path(view: SourceView, deep: bool = False) -> Path:
    return cache_root() / "indexes" / fingerprint(view.spec) / f"{_revision_key(view, deep)}.json"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION and isinstance(value.get("documents"), list) else None


def build(view: SourceView, deep: bool = False) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    for relative in paths(view.path, deep):
        try:
            stat = (view.path / relative).stat()
            text = _read_document(view.path, relative, stat.st_mtime_ns, stat.st_size)
        except (OSError, UnicodeDecodeError):
            continue
        documents.append({"path": relative, "text": text, "size": stat.st_size})
    value = {
        "schema_version": SCHEMA_VERSION,
        "source": view.spec.id,
        "repository": view.spec.repository,
        "commit": view.commit,
        "commit_type": view.commit_type,
        "deep": deep,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "documents": documents,
    }
    destination = index_path(view, deep)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="index-", suffix=".json", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return value


def ensure(view: SourceView, deep: bool = False) -> dict[str, Any]:
    destination = index_path(view, deep)
    if view.dirty:
        # A dirty Git checkout keeps HEAD as its provenance marker, so the
        # cache key alone cannot distinguish two uncommitted edits.
        return build(view, deep)
    value = _load(destination) if destination.exists() else None
    if value and value.get("commit") == view.commit and bool(value.get("deep")) == deep:
        return value
    return build(view, deep)


def status(view: SourceView, deep: bool = False) -> dict[str, Any]:
    destination = index_path(view, deep)
    value = _load(destination) if destination.exists() else None
    indexed_commit = value.get("commit") if value else None
    return {
        "path": str(destination),
        "exists": destination.exists(),
        "indexed_commit": indexed_commit,
        "current_commit": view.commit,
        "stale": bool(indexed_commit and view.commit and indexed_commit != view.commit),
        "document_count": len(value.get("documents", [])) if value else 0,
        "deep": deep,
    }
