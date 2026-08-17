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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discovery import data_root
from local_embedding import (
    EMBEDDING_DIMENSION,
    active_embedding_spec,
    cosine,
    embedding_status,
    pack,
    unpack,
    vectorize,
)
from local_memory import SECRET_CONTENT, STOP_WORDS, _payload, _stable_id
from memos_lifecycle import (
    backpropagate,
    classify_turn,
    feedback_value,
    policy_candidate,
    ready_buckets,
)
from models import MEMORY_LAYERS, memory_layer_state


def dt_from_timestamp(value: object) -> str:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    try:
        numeric = float(value)
        # AML sends Unix milliseconds.  Local runtime records use seconds.
        if abs(numeric) >= 100_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_from_timestamp(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            value = text
    try:
        numeric = float(value)
        if abs(numeric) >= 100_000_000_000:
            numeric /= 1000.0
        return numeric
    except (TypeError, ValueError, OverflowError):
        return None


_LATEST_QUERY = re.compile(
    r"\b(latest|recent|newest|current|most\s+recent|last)\b|最近|最新|当前|上次|最近一次",
    re.IGNORECASE,
)


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
            ("episode_id", "TEXT"),
            ("turn_id", "TEXT"),
            ("value", "REAL"),
            ("priority", "REAL"),
            ("alpha", "REAL NOT NULL DEFAULT 0.3"),
            ("reflection", "TEXT"),
        ):
            if name not in existing:
                connection.execute(f"ALTER TABLE records ADD COLUMN {name} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS records_layer_idx ON records(layer)")
        connection.execute("CREATE INDEX IF NOT EXISTS records_session_idx ON records(session_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS records_episode_idx ON records(episode_id)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS memory_feedback (
                feedback_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                rating TEXT NOT NULL,
                note TEXT NOT NULL,
                agent TEXT,
                created_at REAL NOT NULL,
                delta REAL NOT NULL
            )"""
        )
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
        spec = active_embedding_spec()
        rows = connection.execute(
            """SELECT id, content FROM records
               WHERE embedding IS NULL OR embedding_provider != ?
                  OR embedding_model != ? OR embedding_dim != ?""",
            (spec["provider"], spec["model"], spec["dimension"]),
        ).fetchall()
        for row in rows:
            vector = vectorize(str(row[1]))
            connection.execute(
                "UPDATE records SET embedding=?, embedding_provider=?, embedding_model=?, embedding_dim=? WHERE id=?",
                (pack(vector), spec["provider"], spec["model"], len(vector), str(row[0])),
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
        spec = active_embedding_spec()
        configured_embedding = embedding_status(probe=True)
        connection = self._connect()
        try:
            counts = self._counts(connection)
            fts = self._fts(connection)
            vector_count = int(connection.execute(
                "SELECT COUNT(*) FROM records WHERE embedding_model=? AND embedding_dim=?",
                (spec["model"], spec["dimension"]),
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
                **spec,
                "indexed_records": vector_count,
                "configured_provider": configured_embedding.get("provider"),
                "configured_model": configured_embedding.get("model"),
                "configured_available": configured_embedding.get("available"),
                "configuration_error": configured_embedding.get("error"),
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

    def evolve_policies(self, *, min_distinct_episodes: int = 2) -> dict[str, Any]:
        """Build MemOS-style L2 policy candidates from repeated L1 evidence."""

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, session_id, episode_id, content FROM records WHERE layer='L1' ORDER BY created_at ASC",
            ).fetchall()
            buckets = ready_buckets([dict(row) for row in rows], min_distinct_episodes=max(2, int(min_distinct_episodes)))
            created: list[str] = []
            for bucket in buckets:
                candidate = policy_candidate(bucket)
                signature = str(bucket["signature"])
                path = f"policy/{hashlib.sha256(signature.encode('utf-8')).hexdigest()[:24]}"
                record_id = f"local:L2:{path}"
                content = "\n".join([
                    "status: candidate",
                    "layer: L2",
                    "kind: policy",
                    "generated: true",
                    "",
                    f"trigger: {candidate['trigger']}",
                    "procedure:",
                    *[f"- {step}" for step in candidate["procedure"]],
                    f"verification: {candidate['verification']}",
                    f"boundary: {candidate['boundary']}",
                    f"support_episode_count: {candidate['support']['episode_count']}",
                    f"support_record_ids: {','.join(candidate['support']['record_ids'])}",
                    "evidence:",
                    *[f"- {item}" for item in candidate["evidence"]],
                    "",
                ])
                self._upsert(
                    connection,
                    record_id=record_id,
                    layer="L2",
                    session_id=path,
                    message_index=-1,
                    role="system",
                    content=content,
                    metadata={"pipeline": "memos-candidate-pool", "signature": signature, "source_record_ids": candidate["support"]["record_ids"], "source_episode_ids": bucket["episodes"]},
                    status="candidate",
                    generated=True,
                    accepted=False,
                )
                created.append(record_id)
            connection.commit()
        finally:
            connection.close()
        return {
            "ok": True,
            "backend": self.backend,
            "operation": "evolve-policies",
            "created": len(created),
            "candidate_ids": created,
            "min_distinct_episodes": max(2, int(min_distinct_episodes)),
            "accepted": False,
            "canonical_repo_changed": False,
        }

    def feedback(self, memory_id: str, rating: str, note: str, *, agent: str | None = None, feedback_id: str | None = None) -> dict[str, Any]:
        """Persist feedback and adjust value/priority without deleting evidence."""

        basis = f"{memory_id}|{rating}|{note}|{agent or ''}"
        feedback_id = feedback_id or f"fb:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]}"
        delta = feedback_value(rating)
        connection = self._connect()
        try:
            if connection.execute("SELECT 1 FROM memory_feedback WHERE feedback_id=?", (feedback_id,)).fetchone():
                return {"ok": True, "duplicate": True, "feedback_id": feedback_id, "memory_id": memory_id, "canonical_repo_changed": False}
            connection.execute(
                "INSERT INTO memory_feedback(feedback_id,memory_id,rating,note,agent,created_at,delta) VALUES(?,?,?,?,?,?,?)",
                (feedback_id, memory_id, rating, note, agent, time.time(), delta),
            )
            row = connection.execute("SELECT value FROM records WHERE id=?", (memory_id,)).fetchone()
            if row is not None:
                value = max(-1.0, min(1.0, float(row[0] or 0.0) + delta))
                connection.execute("UPDATE records SET value=?, priority=? WHERE id=?", (value, max(value, 0.0), memory_id))
                episode = connection.execute("SELECT episode_id FROM records WHERE id=?", (memory_id,)).fetchone()
                episode_id = str(episode[0] or "") if episode else ""
                if episode_id:
                    traces = connection.execute("SELECT id, timestamp, alpha FROM records WHERE layer='L1' AND episode_id=? ORDER BY message_index ASC", (episode_id,)).fetchall()
                    updates = backpropagate([
                        {"id": str(trace[0]), "timestamp_epoch": epoch_from_timestamp(trace[1]), "alpha": float(trace[2] or 0.3)}
                        for trace in traces
                    ], value)
                    for update in updates:
                        connection.execute("UPDATE records SET value=?, priority=? WHERE id=?", (update["value"], update["priority"], update["id"]))
            connection.commit()
        finally:
            connection.close()
        return {"ok": True, "duplicate": False, "feedback_id": feedback_id, "memory_id": memory_id, "rating": rating, "delta": delta, "canonical_repo_changed": False}

    def _upsert(self, connection: sqlite3.Connection, *, record_id: str, layer: str, session_id: str, message_index: int, role: str, content: str, timestamp: str = "", metadata: dict[str, Any] | None = None, status: str = "verified", generated: bool = False, accepted: bool = True, episode_id: str | None = None, turn_id: str | None = None, value: float | None = None, priority: float | None = None, alpha: float = 0.3, reflection: str | None = None) -> None:
        now = time.time()
        metadata_value = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        spec = active_embedding_spec()
        vector = vectorize(content)
        embedding = pack(vector)
        connection.execute(
            """INSERT INTO records
            (id, layer, session_id, message_index, role, content, timestamp, metadata, created_at, updated_at, status, generated, accepted, embedding, embedding_provider, embedding_model, embedding_dim, episode_id, turn_id, value, priority, alpha, reflection)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET content=excluded.content, timestamp=excluded.timestamp,
              metadata=excluded.metadata, updated_at=excluded.updated_at, status=excluded.status,
              generated=excluded.generated, accepted=excluded.accepted, embedding=excluded.embedding,
              embedding_provider=excluded.embedding_provider, embedding_model=excluded.embedding_model,
              embedding_dim=excluded.embedding_dim, episode_id=excluded.episode_id, turn_id=excluded.turn_id,
              value=excluded.value, priority=excluded.priority, alpha=excluded.alpha, reflection=excluded.reflection""",
            (record_id, layer, session_id, message_index, role, content, timestamp, metadata_value, now, now, status, int(generated), int(accepted), embedding, spec["provider"], spec["model"], len(vector), episode_id or session_id, turn_id or f"turn:{message_index // 2}", value, priority, alpha, reflection),
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
                episode_id = f"episode:{hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]}"
                for index, message in enumerate(messages):
                    content = message["content"]
                    if SECRET_CONTENT.search(content):
                        skipped_sensitive += 1
                        continue
                    role = message["role"]
                    turn_id = f"{episode_id}:turn:{index // 2}"
                    metadata = {"session_id": session_id, "message_index": index, "episode_id": episode_id, "turn_id": turn_id, "pipeline": "standalone+memos-lifecycle"}
                    self._upsert(connection, record_id=_stable_id("L0", session_id, index, role, content), layer="L0", session_id=session_id, message_index=index, role=role, content=content, timestamp=message["timestamp"], metadata=metadata, episode_id=episode_id, turn_id=turn_id)
                    self._upsert(connection, record_id=_stable_id("L1", session_id, index, role, content), layer="L1", session_id=session_id, message_index=index, role=role, content=content, timestamp=message["timestamp"], metadata=metadata, episode_id=episode_id, turn_id=turn_id, alpha=0.3)
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
            "lifecycle": "memos-style episode/turn boundaries + feedback-weighted candidate pool",
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

    @staticmethod
    def _aml_scope_prefix(user_id: str) -> str:
        """Return a stable, non-reversible storage namespace for an AML user."""

        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        return f"aml:{digest}:"

    def ingest_aml(
        self,
        *,
        request_id: str,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist one AML Add request and make it immediately searchable.

        AML's contract is intentionally narrower than the normal session
        ingest path: the participant receives source messages and must make
        them searchable before returning HTTP 200.  We keep the user scope in
        a hashed session namespace and store only L0/L1 records here; L2/L3
        promotion remains an explicit local lifecycle operation.
        """

        if not request_id or not user_id or not session_id:
            raise ValueError("AML Add requires request_id, user_id and session_id")
        if not isinstance(messages, list) or not messages:
            raise ValueError("AML Add requires a non-empty messages array")
        scope = self._aml_scope_prefix(user_id)
        stored = 0
        skipped_sensitive = 0
        hashed_session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        storage_session = f"{scope}{hashed_session}"
        connection = self._connect()
        try:
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    raise ValueError("AML messages must be objects")
                role = str(message.get("role") or "").strip()
                content = str(message.get("content") or "").strip()
                # The public contract requires a non-empty role but does not
                # restrict it to user/assistant.  Tool, system, and other
                # producer roles are valid memory evidence too.
                if not role or not content:
                    raise ValueError("AML messages require a non-empty role and content")
                if SECRET_CONTENT.search(content):
                    skipped_sensitive += 1
                    continue
                timestamp = str(message.get("timestamp") or "")
                metadata = {
                    "protocol": "agent-memory-leaderboard",
                    "request_id": request_id,
                    "user_id_hash": scope[4:-1],
                    "session_id_hash": hashed_session,
                }
                digest = hashlib.sha256(f"{request_id}\0{index}\0{role}\0{content}".encode("utf-8")).hexdigest()[:32]
                for layer in ("L0", "L1"):
                    self._upsert(
                        connection,
                        record_id=f"aml:{layer}:{digest}",
                        layer=layer,
                        session_id=storage_session,
                        message_index=index,
                        role=role,
                        content=content,
                        timestamp=timestamp,
                        metadata=metadata,
                        status="verified",
                        generated=False,
                        accepted=True,
                    )
                stored += 1
            if not stored:
                raise ValueError("AML Add contained no storable messages")
            connection.commit()
        finally:
            connection.close()
        return {
            "stored_messages": stored,
            "stored_records": stored * 2,
            "skipped_sensitive_messages": skipped_sensitive,
            "readback": {"verified": True, "request_id": request_id},
        }

    def search_aml(self, query: str, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Search only one AML user namespace and return raw searchable memories."""

        terms = list(dict.fromkeys(_terms(query)))
        query_vector = vectorize(query)
        scope = self._aml_scope_prefix(user_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM records WHERE session_id LIKE ? AND layer IN ('L0','L1') "
                "ORDER BY updated_at DESC LIMIT ?",
                (f"{scope}%", max(1000, int(limit) * 200)),
            ).fetchall()
        finally:
            connection.close()
        latest_requested = bool(_LATEST_QUERY.search(query))
        row_epochs = [epoch_from_timestamp(row["timestamp"]) or float(row["updated_at"] or 0) for row in rows]
        min_epoch = min(row_epochs, default=0.0)
        max_epoch = max(row_epochs, default=0.0)
        epoch_span = max(0.0, max_epoch - min_epoch)
        ranked = []
        for row, row_epoch in zip(rows, row_epochs):
            content = str(row["content"])
            matched = [term for term in terms if term in content.casefold()]
            semantic_score = cosine(query_vector, unpack(row["embedding"], int(row["embedding_dim"] or EMBEDDING_DIMENSION)))
            if not matched and semantic_score < 0.12:
                continue
            lexical_score = min(0.65, len(matched) * 0.16)
            recency_bonus = ((row_epoch - min_epoch) / epoch_span) * 0.35 if latest_requested and epoch_span else 0.0
            ranked.append((semantic_score + lexical_score + recency_bonus, str(row["id"]), row, semantic_score))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        results = []
        seen_content: set[str] = set()
        for score, _record_id, row, _semantic_score in ranked:
            content = str(row["content"])
            content_key = content.casefold().strip()
            if content_key in seen_content:
                continue
            seen_content.add(content_key)
            results.append({
                "id": str(row["id"]),
                "content": content,
                "score": round(score, 6),
                "created_at": dt_from_timestamp(row["timestamp"] or row["updated_at"]),
            })
            if len(results) >= max(1, int(limit)):
                break
        return results

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        terms = list(dict.fromkeys(_terms(query)))
        query_vector = vectorize(query)
        ranked = []
        for row in self._rows(query, limit):
            content = str(row["content"])
            matched = [term for term in terms if term in content.casefold()]
            semantic_score = cosine(query_vector, unpack(row["embedding"], int(row["embedding_dim"] or EMBEDDING_DIMENSION)))
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
        # MemOS exposes a stable tier/ref contract for consumers that need to
        # distinguish trace, policy, and world-model results.  Keep our
        # canonical L0-L3 names as the authority and add the compatibility
        # fields instead of making callers infer them from ``kind``.
        tier = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}.get(layer)
        ref_kind = {"L0": "conversation", "L1": "trace", "L2": "policy", "L3": "world_model"}.get(layer, "memory")
        return {
            "id": record_id,
            "kind": {"L0": "conversation", "L1": "atomic", "L2": "scenario", "L3": "profile"}.get(layer, "memory"),
            "title": layer,
            "content": content,
            "excerpt": content,
            "snippet": content[:512],
            "memory_layer": layer,
            "memory_type": {"L0": "conversation", "L1": "atomic", "L2": "scenario", "L3": "profile"}.get(layer, "memory"),
            "tier": tier,
            "ref_kind": ref_kind,
            "ref_id": record_id,
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
            "tier": {"L0": 0, "L1": 1, "L2": 2, "L3": 3}.get(layer),
            "ref_kind": {"L0": "conversation", "L1": "trace", "L2": "policy", "L3": "world_model"}.get(layer, "memory"),
            "ref_id": result_id,
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
        items = []
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata"] or "{}"))
            except json.JSONDecodeError:
                metadata = {}
            item = {"path": str(row["session_id"]), "id": str(row["id"]), "status": str(row["status"]), "content": str(row["content"]), "metadata": metadata}
            source_ids = metadata.get("source_record_ids") if isinstance(metadata, dict) else None
            if isinstance(source_ids, list) and source_ids:
                item["provenance"] = {"citations": [{"memory_id": str(value), "layer": "L1"} for value in source_ids]}
            items.append(item)
        return items

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

    def timeline(self, session_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Return an ordered trace view without changing retrieval ranking.

        This is the useful, lightweight part of MemOS' timeline tool: callers
        can inspect how an L0/L1 memory was formed, while the normal search
        path remains citation-first and layer-aware.
        """

        connection = self._connect()
        try:
            bounded = max(1, min(int(limit), 500))
            if session_id:
                rows = connection.execute(
                    "SELECT * FROM records WHERE session_id=? AND layer IN ('L0','L1') "
                    "ORDER BY message_index ASC, layer ASC, created_at ASC LIMIT ?",
                    (session_id, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM records WHERE layer IN ('L0','L1') "
                    "ORDER BY created_at ASC, message_index ASC LIMIT ?",
                    (bounded,),
                ).fetchall()
        finally:
            connection.close()
        events = []
        for row in rows:
            layer = str(row["layer"])
            record_id = str(row["id"])
            events.append({
                "id": record_id,
                "layer": layer,
                "tier": {"L0": 0, "L1": 1}.get(layer),
                "ref_kind": "conversation" if layer == "L0" else "trace",
                "ref_id": record_id,
                "session_id": str(row["session_id"]),
                "message_index": int(row["message_index"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "snippet": str(row["content"])[:512],
                "timestamp": row["timestamp"],
                "status": str(row["status"]),
                "metadata": json.loads(str(row["metadata"] or "{}")),
            })
        return {
            "schema_version": 1,
            "ok": True,
            "backend": self.backend,
            "session_id": session_id,
            "events": events,
            "count": len(events),
            "readback": {"verified": True, "backend": self.backend},
            "canonical_repo_changed": False,
        }


def standalone_memory_client() -> StandaloneMemoryClient:
    return StandaloneMemoryClient()
