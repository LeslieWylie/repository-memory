#!/usr/bin/env python3
"""Compatibility L0/L1 store used by legacy source adapters.

The public default runtime lives in :mod:`standalone_memory` and owns all four
layers.  This small class remains for old source commands and migration tests;
it deliberately keeps its historical L0/L1-only contract instead of pretending
that a legacy fallback is a full long-term memory implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from discovery import data_root
from models import MEMORY_LAYERS, memory_layer_state
from tokenize_query import fts5_can_match, plane_terms

SECRET_CONTENT = re.compile(
    r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{16,}|\bsk-[A-Za-z0-9_-]{16,}",
    re.IGNORECASE,
)
STOP_WORDS = {
    "what", "when", "where", "which", "with", "from", "this", "that", "about",
    "memory", "conversation", "evidence", "source", "latest", "recent", "and", "or",
}


def _stable_id(layer: str, session_id: str, index: int, role: str, content: str) -> str:
    value = "\0".join((layer, session_id, str(index), role, content)).encode("utf-8")
    return f"local:{layer}:{hashlib.sha256(value).hexdigest()[:24]}"


def _payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict) and isinstance(value.get("sessions"), list):
        return value
    if isinstance(value, dict) and isinstance(value.get("messages"), list):
        return {"sessions": [{"sessionKey": str(value.get("session_id") or value.get("sessionKey") or "local-session"), "messages": value["messages"]}]}
    if isinstance(value, list) and value and all(isinstance(row, dict) and "sessionKey" in row for row in value):
        return {"sessions": value}
    rows = value if isinstance(value, list) else [value]
    return {"sessions": [{"sessionKey": "local-session", "messages": rows}]}


class LocalMemoryStore:
    """SQLite-backed L0/L1 store with deterministic ids and lexical search."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_root() / "memory" / "local.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                layer TEXT NOT NULL,
                session_id TEXT NOT NULL,
                message_index INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT,
                metadata TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS records_layer_idx ON records(layer)")
        connection.execute("CREATE INDEX IF NOT EXISTS records_session_idx ON records(session_id)")
        try:
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED, content)")
        except sqlite3.OperationalError:
            pass
        connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return connection

    def _fts_available(self, connection: sqlite3.Connection) -> bool:
        try:
            connection.execute("SELECT 1 FROM records_fts LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    def health(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            count = int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            counts = {
                str(row[0]): int(row[1])
                for row in connection.execute("SELECT layer, COUNT(*) FROM records GROUP BY layer").fetchall()
            }
            fts = self._fts_available(connection)
        finally:
            connection.close()
        return {
            "backend": "local-memory",
            "supported_layers": ["L0", "L1"],
            "configured": True,
            "reachable": True,
            "status": "ready",
            "path": str(self.path),
            "record_count": count,
            "index": "fts5" if fts else "sqlite-scan",
            "embedding": {"available": False, "strategy": "keyword-only"},
            "layers": {
                layer: memory_layer_state(
                    "supported" if layer in {"L0", "L1"} else "unsupported",
                    "ready" if layer in {"L0", "L1"} else "unsupported",
                    "present" if counts.get(layer, 0) else "empty" if layer in {"L0", "L1"} else "unknown",
                    "verified" if layer in {"L0", "L1"} else "unknown",
                    persistent=layer in {"L0", "L1"},
                    **({"record_count": counts.get(layer, 0)} if layer in {"L0", "L1"} else {}),
                )
                for layer in MEMORY_LAYERS
            },
        }

    @staticmethod
    def _messages(session: dict[str, Any]) -> list[dict[str, str]]:
        raw = session.get("messages")
        if not isinstance(raw, list):
            raw = session.get("conversations")
        if isinstance(raw, list) and raw and isinstance(raw[0], list):
            raw = [item for group in raw if isinstance(group, list) for item in group]
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            if not isinstance(item, dict) or not str(item.get("role") or "").strip() or not str(item.get("content") or "").strip():
                continue
            result.append({
                "role": str(item["role"]),
                "content": str(item["content"]),
                "timestamp": str(item.get("timestamp") or ""),
            })
        return result

    def ingest(self, input_path: Path) -> dict[str, Any]:
        payload = _payload(input_path)
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        if not sessions:
            raise ValueError("session input contains no sessions")
        connection = self._connect()
        written_l0 = 0
        written_l1 = 0
        skipped_sensitive = 0
        session_count = 0
        now = time.time()
        try:
            for position, session in enumerate(sessions):
                if not isinstance(session, dict):
                    continue
                session_id = str(session.get("sessionKey") or session.get("session_id") or f"local-session-{position}")
                messages = self._messages(session)
                if not messages:
                    continue
                session_count += 1
                for index, message in enumerate(messages):
                    content = message["content"]
                    if SECRET_CONTENT.search(content):
                        skipped_sensitive += 1
                        continue
                    role = message["role"]
                    metadata = json.dumps({"session_id": session_id, "message_index": index}, ensure_ascii=False, sort_keys=True)
                    records = [
                        ("L0", _stable_id("L0", session_id, index, role, content), "conversation"),
                        ("L1", _stable_id("L1", session_id, index, role, content), "atomic"),
                    ]
                    for layer, record_id, _kind in records:
                        connection.execute(
                            """INSERT INTO records
                            (id, layer, session_id, message_index, role, content, timestamp, metadata, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                              content=excluded.content, timestamp=excluded.timestamp,
                              metadata=excluded.metadata, updated_at=excluded.updated_at""",
                            (record_id, layer, session_id, index, role, content, message["timestamp"], metadata, now, now),
                        )
                        if self._fts_available(connection):
                            connection.execute("DELETE FROM records_fts WHERE id = ?", (record_id,))
                            connection.execute("INSERT INTO records_fts (id, content) VALUES (?, ?)", (record_id, content))
                        if layer == "L0":
                            written_l0 += 1
                        else:
                            written_l1 += 1
            connection.commit()
        finally:
            connection.close()
        if not session_count:
            raise ValueError("session input contains no valid role/content messages")
        return {
            "backend": "local-memory",
            "pipeline": "local L0 raw conversation -> deterministic L1 atomic",
            "sessions": session_count,
            "l0_recorded": written_l0,
            "l1_recorded": written_l1,
            "l0_verified": written_l0 > 0,
            "l1_status": "verified" if written_l1 else "unknown",
            "skipped_sensitive_messages": skipped_sensitive,
            "verified": written_l0 > 0,
            "canonical_repo_changed": False,
        }

    @staticmethod
    def _terms(query: str) -> list[str]:
        """Tokenize a query the same way every other retrieval plane does.

        This used to be ``[\\w一-龥./:-]{2,}`` with no segmentation, so a
        Chinese question arrived as one clause-length token matching nothing.
        """

        return plane_terms(query, STOP_WORDS)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        terms = list(dict.fromkeys(self._terms(query)))
        if not terms:
            return []
        connection = self._connect()
        try:
            candidates: list[sqlite3.Row]
            # FTS is only a candidate pre-filter; the substring pass below is
            # the real match.  Scan instead of pre-filtering when the index
            # cannot see a term — see ``tokenize_query.fts5_can_match``.
            if self._fts_available(connection) and all(fts5_can_match(term) for term in terms):
                match = " OR ".join('"' + term.replace('"', "") + '"' for term in terms)
                candidates = connection.execute(
                    "SELECT r.* FROM records r JOIN records_fts f ON f.id = r.id WHERE records_fts MATCH ?",
                    (match,),
                ).fetchall()
            else:
                candidates = connection.execute("SELECT * FROM records").fetchall()
        finally:
            connection.close()
        ranked = []
        for row in candidates:
            content = str(row["content"])
            haystack = content.casefold()
            matched = [term for term in terms if term in haystack]
            if not matched:
                continue
            score = len(matched) + (0.1 if row["layer"] == "L1" else 0.0)
            ranked.append((score, str(row["id"]), row, matched))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [self._result(row, matched) for _score, _id, row, matched in ranked[:limit]]

    @staticmethod
    def _result(row: sqlite3.Row, matched: list[str]) -> dict[str, Any]:
        record_id = str(row["id"])
        layer = str(row["layer"])
        content = str(row["content"])
        return {
            "id": record_id,
            "kind": "conversation" if layer == "L0" else "atomic",
            "title": layer,
            "content": content,
            "excerpt": content,
            "memory_layer": layer,
            "memory_type": "conversation" if layer == "L0" else "atomic",
            "score": len(matched),
            "updated_at": row["updated_at"],
            "_native_memory": True,
            "_memory_backend": "local-memory",
            "citation": {
                "source": "local-memory",
                "memory_id": record_id,
                "layer": layer,
                "evidence": content,
                "locator": {"layer": layer, "memory_id": record_id, "session_id": row["session_id"]},
                "valid": True,
                "generated": False,
                "accepted": True,
            },
        }

    def get(self, result_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM records WHERE id = ?", (result_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(f"local memory record not found: {result_id}")
        return {
            "id": result_id,
            "layer": row["layer"],
            "memory": {key: row[key] for key in ("session_id", "message_index", "role", "content", "timestamp", "metadata")},
            "citation": {"source": "local-memory", "memory_id": result_id, "layer": row["layer"], "evidence": row["content"], "valid": True},
        }


def local_memory_store() -> LocalMemoryStore:
    return LocalMemoryStore()
