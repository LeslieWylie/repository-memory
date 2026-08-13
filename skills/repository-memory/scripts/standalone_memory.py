#!/usr/bin/env python3
"""Self-contained, provider-free L0-L3 memory runtime.

This is the default memory implementation for repository-memory.  It is an
in-process SQLite store with FTS5 and a dependency-free local vector index; it
does not require a gateway, Node, an embedding service, or credentials.  The
optional vendor clients remain available only through an explicit external-mode
opt-in.

The lifecycle intentionally mirrors the useful part of the vendor model:

    explicit session -> L0/L1 read-back -> L2 candidate -> explicit accept
    -> L3 profile read-back

L2/L3 are never inferred from an endpoint being reachable.  They are records
in this store and their status is persisted alongside their content.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from discovery import data_root
from local_embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    cosine,
    pack,
    unpack,
    vectorize,
)
from local_memory import SECRET_CONTENT, STOP_WORDS, _payload, _stable_id
from models import MEMORY_LAYERS, memory_layer_state


def _terms(query: str) -> list[str]:
    return [
        term.casefold()
        for term in re.findall(r"[\w一-龥./:-]{2,}", query, re.UNICODE)
        if term.casefold() not in STOP_WORDS
    ]


def _lifecycle(content: str, default: str = "verified") -> str:
    match = re.search(r"^status:\s*(candidate|pending|accepted|generated|stale|verified)\s*$", content, re.MULTILINE | re.IGNORECASE)
    return match.group(1).lower() if match else default


def _marked(content: str, *, status: str, layer: str, source_l2: str | None = None) -> str:
    lines = content.splitlines()
    while lines and re.match(r"^(status|layer|source_l2):", lines[0], re.IGNORECASE):
        lines.pop(0)
    prefix = [f"status: {status}", f"layer: {layer}"]
    if source_l2:
        prefix.append(f"source_l2: {source_l2}")
    return "\n".join([*prefix, "", *lines]).strip() + "\n"


@dataclass(frozen=True)
class StandaloneIdentity:
    team_id: str = "local"
    agent_id: str = "repository-memory"
    user_id: str = "local-user"

    @property
    def identity(self) -> dict[str, str]:
        return {"team_id": self.team_id, "agent_id": self.agent_id, "user_id": self.user_id}


class StandaloneMemoryClient:
    """A durable local implementation of the four memory layers."""

    configured = True
    backend = "standalone-memory"

    def __init__(self, path: Path | None = None):
        self.path = path or (data_root() / "memory" / "local.sqlite3")
        self.identity = {
            "team_id": "local",
            "agent_id": "repository-memory",
            "user_id": "local-user",
        }
        self.config = StandaloneIdentity()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
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
        existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(records)").fetchall()}
        for name, definition in (
            ("status", "TEXT NOT NULL DEFAULT 'verified'"),
            ("generated", "INTEGER NOT NULL DEFAULT 0"),
            ("accepted", "INTEGER NOT NULL DEFAULT 1"),
            ("embedding", "BLOB"),
            ("embedding_provider", "TEXT"),
            ("embedding_model", "TEXT"),
            ("embedding_dim", "INTEGER"),
        ):
            if name not in existing:
                connection.execute(f"ALTER TABLE records ADD COLUMN {name} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS records_layer_idx ON records(layer)")
        connection.execute("CREATE INDEX IF NOT EXISTS records_session_idx ON records(session_id)")
        try:
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED, content)")
        except sqlite3.OperationalError:
            pass
        connection.commit()
        self._backfill_embeddings(connection)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return connection

    @staticmethod
    def _backfill_embeddings(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT id, content FROM records WHERE embedding IS NULL OR embedding_model != ?",
            (EMBEDDING_MODEL,),
        ).fetchall()
        for row in rows:
            vector = vectorize(str(row[1]))
            connection.execute(
                "UPDATE records SET embedding=?, embedding_provider=?, embedding_model=?, embedding_dim=? WHERE id=?",
                (pack(vector), EMBEDDING_PROVIDER, EMBEDDING_MODEL, EMBEDDING_DIMENSION, str(row[0])),
            )
        if rows:
            connection.commit()

    @staticmethod
    def _fts(connection: sqlite3.Connection) -> bool:
        try:
            connection.execute("SELECT 1 FROM records_fts LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _counts(connection: sqlite3.Connection) -> dict[str, int]:
        return {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT layer, COUNT(*) FROM records GROUP BY layer").fetchall()
        }

    @staticmethod
    def _lifecycle_counts(connection: sqlite3.Connection, layer: str) -> dict[str, int]:
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM records WHERE layer=? GROUP BY status",
            (layer,),
        ).fetchall()
        values = {str(row[0]): int(row[1]) for row in rows}
        return {
            "candidate_count": values.get("candidate", 0) + values.get("pending", 0),
            "accepted_count": values.get("accepted", 0),
        }

    def health(self, refresh: bool = False, probe_layers: bool = False) -> dict[str, Any]:
        connection = self._connect()
        try:
            counts = self._counts(connection)
            fts = self._fts(connection)
            vector_count = int(connection.execute(
                "SELECT COUNT(*) FROM records WHERE embedding_model=? AND embedding_dim=?",
                (EMBEDDING_MODEL, EMBEDDING_DIMENSION),
            ).fetchone()[0])
            lifecycle_counts = {
                layer: self._lifecycle_counts(connection, layer)
                for layer in MEMORY_LAYERS
            }
        finally:
            connection.close()
        layers = {}
        for layer in MEMORY_LAYERS:
            count = counts.get(layer, 0)
            layers[layer] = memory_layer_state(
                "supported",
                "ready",
                "present" if count else "empty",
                "verified" if count else "unknown",
                persistent=True,
                record_count=count,
                backend="standalone-memory",
                **lifecycle_counts[layer],
            )
        return {
            "ok": True,
            "backend": self.backend,
            "mode": "in-process",
            "external_dependency": False,
            "supported_layers": list(MEMORY_LAYERS),
            "configured": True,
            "reachable": True,
            "status": "ready",
            "path": str(self.path),
            "record_count": sum(counts.values()),
            "index": "fts5" if fts else "sqlite-scan",
            "embedding": {
                "available": True,
                "strategy": "local-hybrid",
                "provider": EMBEDDING_PROVIDER,
                "model": EMBEDDING_MODEL,
                "dimension": EMBEDDING_DIMENSION,
                "indexed_records": vector_count,
                "native_neural_model": False,
            },
            "llm": {"configured": False, "provider": None, "model": None},
            "runtime": {"process": "in-process", "service_required": False, "data_dir": str(self.path.parent)},
            "layers": layers,
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
        return [
            {"role": str(item["role"]), "content": str(item["content"]), "timestamp": str(item.get("timestamp") or "")}
            for item in raw
            if isinstance(item, dict) and str(item.get("role") or "").strip() and str(item.get("content") or "").strip()
        ]

    @staticmethod
    def _candidate_content(session_id: str, messages: list[dict[str, str]]) -> str:
        """Build a reviewable L2 scenario without claiming it is accepted."""

        selected = []
        for message in messages:
            content = " ".join(message["content"].split()).strip()
            if not content or SECRET_CONTENT.search(content):
                continue
            selected.append(f"- {message['role']}: {content[:500]}")
        selected = selected[-8:]
        return "\n".join([
            "status: candidate",
            "layer: L2",
            "generated: true",
            f"session_id: {session_id}",
            "",
            "# Scenario candidate",
            "",
            *selected,
        ]).strip() + "\n"

    def _project_candidate(self, connection: sqlite3.Connection, session_id: str, messages: list[dict[str, str]]) -> str | None:
        if not messages:
            return None
        path = f"session/{session_id}"
        existing = connection.execute(
            "SELECT id FROM records WHERE layer='L2' AND session_id=?",
            (path,),
        ).fetchone()
        if existing:
            return str(existing[0])
        record_id = f"local:L2:{hashlib.sha256(path.encode()).hexdigest()[:24]}"
        self._upsert(
            connection,
            record_id=record_id,
            layer="L2",
            session_id=path,
            message_index=-1,
            role="system",
            content=self._candidate_content(session_id, messages),
            metadata={"path": path, "session_id": session_id, "pipeline": "standalone-session-projection"},
            status="candidate",
            generated=True,
            accepted=False,
        )
        return record_id

    def project_candidates(self) -> dict[str, Any]:
        """Project existing L0 conversations into reviewable L2 candidates."""

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM records WHERE layer='L0' ORDER BY session_id, message_index",
            ).fetchall()
            grouped: dict[str, list[dict[str, str]]] = {}
            for row in rows:
                grouped.setdefault(str(row["session_id"]), []).append({
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "timestamp": str(row["timestamp"] or ""),
                })
            ids = [self._project_candidate(connection, session_id, messages) for session_id, messages in grouped.items()]
            connection.commit()
        finally:
            connection.close()
        return {
            "ok": True,
            "backend": self.backend,
            "projected": len([item for item in ids if item]),
            "candidate_ids": [item for item in ids if item],
            "status": "candidate",
            "accepted": False,
            "canonical_repo_changed": False,
        }

    def _upsert(self, connection: sqlite3.Connection, *, record_id: str, layer: str, session_id: str, message_index: int, role: str, content: str, timestamp: str = "", metadata: dict[str, Any] | None = None, status: str = "verified", generated: bool = False, accepted: bool = True) -> None:
        now = time.time()
        metadata_value = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        embedding = pack(vectorize(content))
        connection.execute(
            """INSERT INTO records
            (id, layer, session_id, message_index, role, content, timestamp, metadata, created_at, updated_at, status, generated, accepted, embedding, embedding_provider, embedding_model, embedding_dim)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET content=excluded.content, timestamp=excluded.timestamp,
              metadata=excluded.metadata, updated_at=excluded.updated_at, status=excluded.status,
              generated=excluded.generated, accepted=excluded.accepted, embedding=excluded.embedding,
              embedding_provider=excluded.embedding_provider, embedding_model=excluded.embedding_model,
              embedding_dim=excluded.embedding_dim""",
            (record_id, layer, session_id, message_index, role, content, timestamp, metadata_value, now, now, status, int(generated), int(accepted), embedding, EMBEDDING_PROVIDER, EMBEDDING_MODEL, EMBEDDING_DIMENSION),
        )
        if self._fts(connection):
            connection.execute("DELETE FROM records_fts WHERE id = ?", (record_id,))
            connection.execute("INSERT INTO records_fts (id, content) VALUES (?, ?)", (record_id, content))

    def ingest(self, input_path: Path) -> dict[str, Any]:
        payload = _payload(input_path)
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        if not sessions:
            raise ValueError("session input contains no sessions")
        connection = self._connect()
        written_l0 = written_l1 = skipped_sensitive = session_count = l2_candidates = 0
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
                    metadata = {"session_id": session_id, "message_index": index, "pipeline": "standalone"}
                    self._upsert(connection, record_id=_stable_id("L0", session_id, index, role, content), layer="L0", session_id=session_id, message_index=index, role=role, content=content, timestamp=message["timestamp"], metadata=metadata)
                    self._upsert(connection, record_id=_stable_id("L1", session_id, index, role, content), layer="L1", session_id=session_id, message_index=index, role=role, content=content, timestamp=message["timestamp"], metadata=metadata)
                    written_l0 += 1
                    written_l1 += 1
                if messages:
                    l2_candidates += 1 if self._project_candidate(connection, session_id, messages) else 0
            connection.commit()
        finally:
            connection.close()
        if not session_count:
            raise ValueError("session input contains no valid role/content messages")
        return {
            "backend": self.backend,
            "pipeline": "standalone L0 raw conversation -> deterministic L1 atomic",
            "sessions": session_count,
            "l0_recorded": written_l0,
            "l1_recorded": written_l1,
            "l0_verified": written_l0 > 0,
            "l1_status": "verified" if written_l1 else "unknown",
            "l2_status": "candidate" if l2_candidates else "pending_explicit_review",
            "l2_candidates": l2_candidates,
            "l3_status": "explicit_promotion_only",
            "skipped_sensitive_messages": skipped_sensitive,
            "verified": written_l0 > 0,
            "canonical_repo_changed": False,
        }

    def _rows(self, query: str, limit: int) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            # Semantic search needs the full candidate set; SQLite FTS is still
            # used as a cheap lexical signal during ranking.
            return connection.execute(
                "SELECT * FROM records ORDER BY updated_at DESC LIMIT ?",
                (max(1000, int(limit) * 200),),
            ).fetchall()
        finally:
            connection.close()

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        terms = list(dict.fromkeys(_terms(query)))
        query_vector = vectorize(query)
        ranked = []
        for row in self._rows(query, limit):
            content = str(row["content"])
            matched = [term for term in terms if term in content.casefold()]
            semantic_score = cosine(query_vector, unpack(row["embedding"]))
            if not matched and semantic_score < 0.12:
                continue
            layer = str(row["layer"])
            bonus = {"L3": 0.45, "L2": 0.35, "L1": 0.2, "L0": 0.0}.get(layer, 0.0)
            lexical_score = min(0.65, len(matched) * 0.16)
            score = semantic_score + lexical_score + bonus
            ranked.append((score, str(row["id"]), row, matched, semantic_score))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [self._result(row, matched, semantic_score) for _score, _id, row, matched, semantic_score in ranked[: max(1, int(limit))]]

    def _result(self, row: sqlite3.Row, matched: list[str], semantic_score: float = 0.0) -> dict[str, Any]:
        record_id = str(row["id"])
        layer = str(row["layer"])
        content = str(row["content"])
        status = str(row["status"] or ("accepted" if row["accepted"] else "candidate"))
        accepted = bool(row["accepted"]) and status == "accepted" if layer in {"L2", "L3"} else True
        generated = bool(row["generated"])
        return {
            "id": record_id,
            "kind": {"L0": "conversation", "L1": "atomic", "L2": "scenario", "L3": "profile"}.get(layer, "memory"),
            "title": layer,
            "content": content,
            "excerpt": content,
            "memory_layer": layer,
            "memory_type": {"L0": "conversation", "L1": "atomic", "L2": "scenario", "L3": "profile"}.get(layer, "memory"),
            "status": status,
            "generated": generated,
            "accepted": accepted,
            "score": round(semantic_score + min(0.65, len(matched) * 0.16), 6),
            "semantic_score": round(semantic_score, 6),
            "retrieval_mode": "local-hybrid",
            "updated_at": row["updated_at"],
            "_native_memory": True,
            "_memory_backend": self.backend,
            "citation": {
                "source": self.backend,
                "memory_id": record_id,
                "layer": layer,
                "evidence": content,
                "locator": {"layer": layer, "memory_id": record_id, "session_id": row["session_id"]},
                "valid": True,
                "generated": generated,
                "accepted": accepted,
                "provenance": {"source": self.backend, "session_id": row["session_id"], "metadata": row["metadata"]},
            },
        }

    def get(self, result_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM records WHERE id = ?", (result_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(f"standalone memory record not found: {result_id}")
        layer = str(row["layer"])
        content = str(row["content"])
        accepted = bool(row["accepted"]) and str(row["status"]) == "accepted" if layer in {"L2", "L3"} else True
        return {
            "id": result_id,
            "layer": layer,
            "status": str(row["status"]),
            "memory": {"session_id": row["session_id"], "message_index": row["message_index"], "role": row["role"], "content": content, "timestamp": row["timestamp"], "metadata": row["metadata"], "status": str(row["status"]), "accepted": accepted},
            "citation": {"source": self.backend, "memory_id": result_id, "layer": layer, "evidence": content, "locator": {"layer": layer, "memory_id": result_id}, "valid": True, "generated": bool(row["generated"]), "accepted": accepted},
            "readback": {"verified": True, "status": "verified", "backend": self.backend, "id": result_id},
        }

    def observe_l1(self, session_id: str, limit: int = 100, not_before: str | None = None) -> dict[str, Any]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM records WHERE layer='L1' AND session_id=? ORDER BY message_index LIMIT ?", (session_id, max(1, limit))).fetchall()
        finally:
            connection.close()
        return {"status": "verified" if rows else "pending", "count": len(rows), "record_ids": [str(row["id"]) for row in rows]}

    def list_scenarios(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM records WHERE layer='L2' ORDER BY updated_at DESC").fetchall()
        finally:
            connection.close()
        return [{"path": str(row["session_id"]), "id": str(row["id"]), "status": str(row["status"]), "content": str(row["content"])} for row in rows]

    def scenario_snapshot(self) -> dict[str, str]:
        return {str(row["path"]): hashlib.sha256(str(row["content"]).encode()).hexdigest() for row in self.list_scenarios()}

    def wait_for_scenario(self, before: dict[str, str], timeout: float = 0, poll: float = 0.5) -> dict[str, Any] | None:
        for row in self.list_scenarios():
            digest = hashlib.sha256(str(row["content"]).encode()).hexdigest()
            if before.get(str(row["path"])) != digest:
                return {"path": str(row["path"]), "record": {"content": row["content"]}, "status": row["status"]}
        return None

    def read_scenario(self, path: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM records WHERE layer='L2' AND session_id=?", (path,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(f"standalone scenario not found: {path}")
        return {"path": path, "content": row["content"], "status": row["status"], "id": row["id"]}

    def write_scenario(self, path: str, content: str, summary: str | None = None) -> dict[str, Any]:
        record_id = f"local:L2:{hashlib.sha256(path.encode()).hexdigest()[:24]}"
        status = _lifecycle(content, "candidate")
        connection = self._connect()
        try:
            self._upsert(connection, record_id=record_id, layer="L2", session_id=path, message_index=-1, role="system", content=content, metadata={"path": path, "summary": summary, "pipeline": "standalone"}, status=status, generated=status != "accepted", accepted=status == "accepted")
            connection.commit()
        finally:
            connection.close()
        return {"path": path, "content": content, "status": status, "id": record_id}

    def delete_scenario(self, path: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            result = connection.execute("DELETE FROM records WHERE layer='L2' AND session_id=?", (path,))
            connection.commit()
        finally:
            connection.close()
        return {"deleted_count": result.rowcount}

    def read_core(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM records WHERE id='local:L3:profile'").fetchone()
        finally:
            connection.close()
        if row is None:
            return {"content": "", "status": "empty", "id": "local:L3:profile"}
        return {"content": row["content"], "status": row["status"], "id": row["id"]}

    def write_core(self, content: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            self._upsert(connection, record_id="local:L3:profile", layer="L3", session_id="core/profile", message_index=-1, role="system", content=content, metadata={"path": "core/profile", "pipeline": "standalone"}, status=_lifecycle(content, "accepted"), generated=False, accepted=True)
            connection.commit()
        finally:
            connection.close()
        return self.read_core()

    def delete_conversation(self, message_ids: list[str]) -> dict[str, Any]:
        connection = self._connect()
        try:
            deleted = 0
            for record_id in message_ids:
                deleted += connection.execute("DELETE FROM records WHERE id=? AND layer IN ('L0','L1')", (record_id,)).rowcount
            connection.commit()
        finally:
            connection.close()
        return {"deleted_count": deleted}

    def search_all(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self.search(query, limit)


def standalone_memory_client() -> StandaloneMemoryClient:
    return StandaloneMemoryClient()
