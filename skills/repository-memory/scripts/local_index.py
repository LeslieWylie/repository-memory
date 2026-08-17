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
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from discovery import cache_root, fingerprint
from fallback import _read_document, paths

from models import SourceView

# The JSON document shape is unchanged; keep the cache compatible so an
# upgrade only builds the small path companion FTS instead of rereading every
# document in a large repository.
SCHEMA_VERSION = 5
PATH_FTS_SCHEMA_VERSION = 1
FTS_DOCUMENT_THRESHOLD = 1000

_DATE_RE = re.compile(r"20\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?|20\d{2}-W\d{1,2}", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_INLINE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])(?:[^\s`'\"()<>]+/)?[^\s`'\"()<>]+\.(?:md|mdx|txt|rst|yaml|yml|json)(?:#[^\s`'\"()<>]+)?", re.IGNORECASE)


def _revision_key(view: SourceView, deep: bool) -> str:
    revision = str(view.commit or "unknown")
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in revision)
    return f"{safe or 'unknown'}-{'deep' if deep else 'default'}"


def index_path(view: SourceView, deep: bool = False) -> Path:
    return cache_root() / "indexes" / fingerprint(view.spec) / f"{_revision_key(view, deep)}.json"


def fts_path(view: SourceView, deep: bool = False) -> Path:
    return Path(f"{index_path(view, deep)}.fts.sqlite3")


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION and isinstance(value.get("documents"), list) else None


def _document_dates(relative: str, text: str) -> list[str]:
    """Extract conservative temporal anchors for latest/history routing."""

    anchors = [relative]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "date:", "Date:", "observed_at:", "review_date:")):
            anchors.append(stripped)
    return list(dict.fromkeys(_DATE_RE.findall("\n".join(anchors))))


def _reference_targets(relative: str, text: str, known_paths: set[str]) -> list[str]:
    """Resolve explicit local Markdown/path references into a tiny graph."""

    raw_targets = list(_MARKDOWN_LINK_RE.findall(text))
    raw_targets.extend(_INLINE_PATH_RE.findall(text))
    resolved: list[str] = []
    parent = Path(relative).parent
    for raw in raw_targets:
        target = unquote(str(raw).split("#", 1)[0].split("?", 1)[0]).strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        candidates = [
            Path(target).as_posix().lstrip("./"),
            (parent / target).as_posix(),
        ]
        match = next((candidate for candidate in candidates if candidate in known_paths), None)
        if match and match != relative and match not in resolved:
            resolved.append(match)
    return resolved[:32]


def _ensure_fts(destination: Path, documents: list[dict[str, Any]]) -> Path:
    """Build or reuse the content trigram index.

    Keep this large index content-only so an upgrade does not rewrite tens of
    thousands of full documents.  Filename matching is provided by the small
    companion index in ``_ensure_path_fts``.
    """

    database = Path(f"{destination}.fts.sqlite3")
    if database.exists():
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                connection.execute("SELECT 1 FROM paths LIMIT 1").fetchone()
            finally:
                connection.close()
            return database
        except sqlite3.Error:
            # Replace an older, content-bearing cache with the compact format
            # below. It is derived state and can always be rebuilt from JSON.
            pass
    temporary = Path(tempfile.mkdtemp(prefix="fts-", dir=destination.parent)) / database.name
    try:
        connection = sqlite3.connect(temporary)
        try:
            # Contentless FTS stores only the trigram index. The source text is
            # already in the JSON cache and is never returned from this
            # candidate index; ``paths`` maps FTS rowids back to source paths.
            connection.execute("CREATE TABLE paths(rowid INTEGER PRIMARY KEY, path TEXT NOT NULL)")
            connection.execute("CREATE VIRTUAL TABLE documents USING fts5(text, tokenize='trigram', content='')")
            connection.executemany(
                "INSERT INTO paths(rowid, path) VALUES (?, ?)",
                ((rowid, str(item.get("path") or "")) for rowid, item in enumerate(documents, 1) if item.get("path")),
            )
            connection.executemany(
                "INSERT INTO documents(rowid, text) VALUES (?, ?)",
                ((rowid, str(item.get("text") or "")) for rowid, item in enumerate(documents, 1) if item.get("path")),
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
        temporary.parent.rmdir()
    return database


def _ensure_path_fts(destination: Path, documents: list[dict[str, Any]]) -> Path:
    """Build a tiny trigram index over relative paths only.

    This borrows the useful short-CJK/path fallback behavior without forcing
    a full content-index rebuild on every existing large repository cache.
    """

    database = Path(f"{destination}.fts.paths.v{PATH_FTS_SCHEMA_VERSION}.sqlite3")
    if database.exists():
        return database
    temporary = Path(tempfile.mkdtemp(prefix="fts-paths-", dir=destination.parent)) / database.name
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("CREATE TABLE paths(rowid INTEGER PRIMARY KEY, path TEXT NOT NULL)")
            connection.execute("CREATE VIRTUAL TABLE documents USING fts5(text, tokenize='trigram', content='')")
            rows = [(rowid, str(item.get("path") or "")) for rowid, item in enumerate(documents, 1) if item.get("path")]
            connection.executemany("INSERT INTO paths(rowid, path) VALUES (?, ?)", rows)
            connection.executemany("INSERT INTO documents(rowid, text) VALUES (?, ?)", rows)
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
        temporary.parent.rmdir()
    return database


def build(view: SourceView, deep: bool = False) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    relative_paths = paths(view.path, deep)
    known_paths = set(relative_paths)
    for relative in relative_paths:
        try:
            stat = (view.path / relative).stat()
            text = _read_document(view.path, relative, stat.st_mtime_ns, stat.st_size)
        except (OSError, UnicodeDecodeError):
            continue
        documents.append({
            "path": relative,
            "text": text,
            "size": stat.st_size,
            "dates": _document_dates(relative, text),
            "links": _reference_targets(relative, text, known_paths),
        })
    value = {
        "schema_version": SCHEMA_VERSION,
        "source": view.spec.id,
        "repository": view.spec.repository,
        "commit": view.commit,
        "commit_type": view.commit_type,
        "deep": deep,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "documents": documents,
        "document_count": len(documents),
        "text_bytes": sum(len(str(item.get("text") or "").encode("utf-8")) for item in documents),
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
    if len(documents) >= FTS_DOCUMENT_THRESHOLD:
        value["fts_path"] = str(_ensure_fts(destination, documents))
        value["fts_path_paths"] = str(_ensure_path_fts(destination, documents))
    value["index_bytes"] = destination.stat().st_size if destination.exists() else 0
    return value


def ensure(view: SourceView, deep: bool = False) -> dict[str, Any]:
    destination = index_path(view, deep)
    if view.dirty:
        # A dirty Git checkout keeps HEAD as its provenance marker, so the
        # cache key alone cannot distinguish two uncommitted edits.
        return build(view, deep)
    value = _load(destination) if destination.exists() else None
    if value and value.get("commit") == view.commit and bool(value.get("deep")) == deep:
        # Older caches remain readable.  These fields avoid an O(document_count)
        # text-size scan on every query and are only computed once per process.
        value.setdefault("document_count", len(value.get("documents", [])))
        value.setdefault("text_bytes", sum(len(str(item.get("text") or "").encode("utf-8")) for item in value.get("documents", []) if isinstance(item, dict)))
        value.setdefault("index_bytes", destination.stat().st_size if destination.exists() else 0)
        if len(value.get("documents", [])) >= FTS_DOCUMENT_THRESHOLD:
            value["fts_path"] = str(_ensure_fts(destination, value["documents"]))
            value["fts_path_paths"] = str(_ensure_path_fts(destination, value["documents"]))
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
        "text_bytes": int(value.get("text_bytes") or 0) if value else 0,
        "index_bytes": int(value.get("index_bytes") or destination.stat().st_size if value and destination.exists() else 0),
        "scale_class": "small" if not value or len(value.get("documents", [])) < 1000 else "medium" if len(value.get("documents", [])) < 10000 else "large",
        "deep": deep,
    }
