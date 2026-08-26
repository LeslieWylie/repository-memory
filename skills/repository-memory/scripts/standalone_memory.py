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
from tokenize_query import plane_terms
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

# These are intentionally conservative defaults.  They are exposed in the
# result diagnostics rather than hidden behind a provider-specific reranker.
# The goal is to keep relevant evidence first while suppressing repeated
# copies of the same turn, which is the useful part of MMR for a local store.
_MMR_RELEVANCE_WEIGHT = 0.82
_MMR_DIVERSITY_WEIGHT = 0.18
_RECENCY_HALF_LIFE_DAYS = 14.0


_INTERROGATIVE_ENDINGS = ("?", "？")


def is_question_turn(role: str | None, content: str | None) -> bool:
    """True when a stored turn is a user asking rather than anyone answering.

    A chat-captured store accumulates the questions alongside the answers, and
    a question is the single best lexical *and* semantic match for itself.
    Measured on this store: eleven verbatim copies of "octo-daemon 升级到哪个
    版本了？当时是怎么验证的？" scored 1.65 and took ranks 1-11, while the four
    assistant turns that actually hold "0.5.0 / commit fcec9177" scored ~1.03
    and landed at ranks 24-27.  Recall fetches five, so the answer was
    unreachable — and every replay of the question writes another copy, so the
    margin widens each time it is asked.

    No ranking weight fixes that: MMR's diversity term is 0.18 and the gap is
    0.6.  The exclusion has to happen before the pool is cut.  The test is
    punctuation and role, not vocabulary — there is no question-word list to
    keep in sync with the languages people actually type.  A user turn that
    states a fact ("我们升级到了 0.5.0") is not a question and stays.
    """

    if str(role or "").strip().casefold() != "user":
        return False
    return str(content or "").strip().endswith(_INTERROGATIVE_ENDINGS)


def _is_accepted(row: sqlite3.Row) -> bool:
    """True when a record has been through the lifecycle, not merely proposed.

    L0/L1 capture lands as ``verified``; L2/L3 promotion sets ``accepted``.  A
    record still marked ``candidate`` is a proposal awaiting review, so it has
    not earned the promotion bonus its layer label would otherwise grant.
    """

    keys = row.keys()
    if "accepted" in keys and row["accepted"]:
        return True
    status = str((row["status"] if "status" in keys else "") or "").strip().casefold()
    return status in {"accepted", "verified"}


def _terms(query: str) -> list[str]:
    """Tokenize a query the same way every other retrieval plane does.

    The word regex this used to rely on splits on whitespace and punctuation,
    which works for languages that put spaces between words.  Chinese does not,
    so a whole clause came back as a single token: "octo-daemon 升级到哪个版本
    了?当时是怎么验证的?" tokenized to ``['octo-daemon', '升级到哪个版本了',
    '当时是怎么验证的']``.  Those clause-length strings appear verbatim in
    exactly one place — the stored copy of that same question — so a question
    could only ever lexically match itself.  Measured live: the assistant turns
    holding the answer matched on ``octo-daemon`` alone and scored 0.16 against
    the echo's 0.48.

    Character bigrams were the first fix, and they did recall the answer.  Word
    segmentation replaces them because it recalls the same answer with far less
    noise, and because keeping four planes on four tokenizers meant a query
    behaved differently depending on which one answered it.  Note that the
    unsegmented run is no longer kept as a term: it was precisely the token that
    could only match the echo, and ``is_question_turn`` now drops that echo on a
    principled basis rather than by out-scoring it.
    """

    return plane_terms(query, STOP_WORDS)


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
        connection.execute(
            """CREATE TABLE IF NOT EXISTS memory_links (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                PRIMARY KEY(source_id, target_id, relation)
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS memory_links_source_idx ON memory_links(source_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS memory_links_target_idx ON memory_links(target_id)")
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
        metadata = metadata or {}
        spec = active_embedding_spec()
        raw_retrieval_keys = metadata.get("retrieval_keys") if isinstance(metadata.get("retrieval_keys"), list) else []
        metadata["retrieval_keys"] = [str(value) for value in raw_retrieval_keys if str(value).strip() and not SECRET_CONTENT.search(str(value))]
        metadata_value = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
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
        self._sync_links(connection, record_id, metadata)

    @staticmethod
    def _row_metadata(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(str(row["metadata"] or "{}"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _lookup_key(cls, row: sqlite3.Row, message_index: int | None = None) -> tuple[str, str, str, int]:
        metadata = cls._row_metadata(row)
        batch = str(metadata.get("ingest_id") or metadata.get("request_id") or "legacy")
        return (str(row["session_id"]), str(row["layer"]), batch, int(row["message_index"]) if message_index is None else message_index)

    @classmethod
    def _row_lookup(cls, rows: list[sqlite3.Row]) -> dict[tuple[str, str, str, int], sqlite3.Row]:
        lookup: dict[tuple[str, str, str, int], sqlite3.Row | None] = {}
        for row in rows:
            key = cls._lookup_key(row)
            lookup[key] = row if key not in lookup else None
        return {key: row for key, row in lookup.items() if row is not None}

    @classmethod
    def _retrieval_keys(cls, row: sqlite3.Row, lookup: dict[tuple[str, str, str, int], sqlite3.Row]) -> list[str]:
        metadata = cls._row_metadata(row)
        raw_keys = metadata.get("retrieval_keys") if isinstance(metadata, dict) else []
        keys = [str(value).strip() for value in raw_keys if str(value).strip() and not SECRET_CONTENT.search(str(value))] if isinstance(raw_keys, list) else []
        if str(row["role"] or "").casefold() == "assistant":
            previous = lookup.get(cls._lookup_key(row, int(row["message_index"]) - 1))
            if previous is not None and str(previous["role"] or "").casefold() == "user":
                question = str(previous["content"] or "").strip()
                if question and not SECRET_CONTENT.search(question) and question not in keys:
                    keys.append(question)
        return keys[:3]

    @classmethod
    def _adjacent_context(cls, row: sqlite3.Row, lookup: dict[tuple[str, str, str, int], sqlite3.Row]) -> list[dict[str, Any]]:
        context = []
        index = int(row["message_index"])
        for adjacent_index in (index - 1, index + 1):
            adjacent = lookup.get(cls._lookup_key(row, adjacent_index))
            if adjacent is not None:
                context.append({"id": str(adjacent["id"]), "role": str(adjacent["role"] or ""), "content": str(adjacent["content"] or "")[:512], "message_index": int(adjacent["message_index"])})
        return context

    @staticmethod
    def _sync_links(connection: sqlite3.Connection, record_id: str, metadata: dict[str, Any]) -> None:
        """Persist only explicit provenance/relationship edges.

        This is the lightweight Cognee-style graph seam: links are derived from
        IDs already present in metadata, never guessed from embedding
        similarity.  Replaying an ingest is therefore idempotent and every
        edge remains explainable through its source and target records.
        """

        relation_sets = (
            ("source_record_ids", "supports", "source"),
            ("related_ids", "related", "related"),
        )
        for field, relation, direction in relation_sets:
            values = metadata.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                other = str(value or "").strip()
                if not other or other == record_id:
                    continue
                source_id, target_id = (other, record_id) if direction == "source" else (record_id, other)
                connection.execute(
                    "INSERT OR IGNORE INTO memory_links(source_id,target_id,relation,metadata,created_at) VALUES(?,?,?,?,?)",
                    (source_id, target_id, relation, json.dumps({"field": field}, sort_keys=True), time.time()),
                )

    @staticmethod
    def _related(connection: sqlite3.Connection, record_id: str, limit: int = 8) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT l.relation, CASE WHEN l.source_id=? THEN l.target_id ELSE l.source_id END AS related_id,
                      r.layer, r.content, r.session_id
               FROM memory_links l
               LEFT JOIN records r ON r.id = CASE WHEN l.source_id=? THEN l.target_id ELSE l.source_id END
               WHERE (l.source_id=? OR l.target_id=?)
               ORDER BY l.created_at DESC
               LIMIT ?""",
            (record_id, record_id, record_id, record_id, max(1, min(int(limit), 20))),
        ).fetchall()
        return [
            {
                "id": str(row["related_id"]),
                "relation": str(row["relation"]),
                "layer": str(row["layer"] or "unknown"),
                "excerpt": str(row["content"] or "")[:512],
                "session_id": str(row["session_id"] or ""),
                "citation_valid": bool(row["content"]),
            }
            for row in rows
        ]

    @staticmethod
    def _recency_score(row: sqlite3.Row, *, now: float, latest_requested: bool) -> float:
        epoch = epoch_from_timestamp(row["timestamp"]) or float(row["updated_at"] or now)
        age_days = max(0.0, (now - epoch) / 86400.0)
        decay = 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)
        # Normal queries get only a small freshness tie-break; explicit latest
        # queries get the full signal while relevance remains dominant.
        return decay * (0.32 if latest_requested else 0.08)

    @staticmethod
    def _mmr_select(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        """Select relevant but non-duplicate memories with deterministic MMR."""

        remaining = list(candidates)
        selected: list[dict[str, Any]] = []
        while remaining and len(selected) < max(1, int(limit)):
            best_index = 0
            best_value = float("-inf")
            for index, candidate in enumerate(remaining):
                relevance = float(candidate.get("_relevance", 0.0))
                redundancy = 0.0
                vector = candidate.get("_vector")
                if selected and vector is not None:
                    redundancy = max(
                        cosine(vector, chosen.get("_vector"))
                        for chosen in selected
                        if chosen.get("_vector") is not None
                    )
                value = _MMR_RELEVANCE_WEIGHT * relevance - _MMR_DIVERSITY_WEIGHT * redundancy
                marker = str(candidate.get("id") or "")
                best_marker = str(remaining[best_index].get("id") or "")
                if value > best_value or (value == best_value and marker < best_marker):
                    best_index, best_value = index, value
            selected.append(remaining.pop(best_index))
        for item in selected:
            item["relevance"] = item.get("_relevance", 0.0)
            item.pop("_vector", None)
            item.pop("_relevance", None)
        return selected

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
                ingest_basis = json.dumps(messages, ensure_ascii=False, sort_keys=True)
                ingest_id = hashlib.sha256(f"{session_id}\0{ingest_basis}".encode("utf-8")).hexdigest()[:24]
                for index, message in enumerate(messages):
                    content = message["content"]
                    if SECRET_CONTENT.search(content):
                        skipped_sensitive += 1
                        continue
                    role = message["role"]
                    turn_id = f"{episode_id}:turn:{index // 2}"
                    retrieval_keys = []
                    if role.casefold() == "assistant" and index > 0 and messages[index - 1]["role"].casefold() == "user" and not SECRET_CONTENT.search(messages[index - 1]["content"]):
                        retrieval_keys = [messages[index - 1]["content"]]
                    metadata = {"session_id": session_id, "message_index": index, "episode_id": episode_id, "ingest_id": ingest_id, "turn_id": turn_id, "pipeline": "standalone+memos-lifecycle", "retrieval_keys": retrieval_keys}
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
                if role.casefold() == "assistant" and index > 0:
                    previous = messages[index - 1]
                    previous_content = str(previous.get("content") or "").strip() if isinstance(previous, dict) else ""
                    if isinstance(previous, dict) and str(previous.get("role") or "").casefold() == "user" and not SECRET_CONTENT.search(previous_content):
                        metadata["retrieval_keys"] = [previous_content]
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
        lookup = self._row_lookup(rows)
        for row, row_epoch in zip(rows, row_epochs):
            content = str(row["content"])
            searchable_text = "\n".join([content, *self._retrieval_keys(row, lookup)]).casefold()
            matched = [term for term in terms if term in searchable_text]
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
        latest_requested = bool(_LATEST_QUERY.search(query))
        now = time.time()
        ranked = []
        seen_content: dict[str, dict[str, Any]] = {}
        rows = self._rows(query, limit)
        lookup = self._row_lookup(rows)
        for row in rows:
            content = str(row["content"])
            # Drop the question before it can compete — see ``is_question_turn``.
            # Everything excluded here is already discarded downstream by the
            # answer surface, so this widens what reaches the caller; it cannot
            # remove a result that used to be served.
            if is_question_turn(row["role"], content):
                continue
            retrieval_keys = self._retrieval_keys(row, lookup)
            searchable_text = "\n".join([content, *retrieval_keys]).casefold()
            matched = [term for term in terms if term in searchable_text]
            semantic_score = cosine(query_vector, unpack(row["embedding"], int(row["embedding_dim"] or EMBEDDING_DIMENSION)))
            if not matched and semantic_score < 0.12:
                continue
            layer = str(row["layer"])
            # The promotion premium is paid for having been promoted, so it has
            # to be gated on promotion actually having happened.  Paying it on
            # the layer label alone gave all 45 unaccepted L2 candidates +0.35
            # against an accepted L1 turn's +0.2 — a head start for generated
            # text nobody accepted.  Measured on this store: once the query
            # echoes were excluded, ten candidate envelopes ("status: candidate
            # / generated: true") took ranks 1-10 at ~1.15 and pushed the
            # accepted answers at ~1.03 out of a five-deep recall window, even
            # though the answer surface discards candidates on sight.  An
            # unaccepted candidate is a proposal about L1 evidence, so it scores
            # as that evidence rather than as the tier it aspires to.
            promoted = _is_accepted(row)
            bonus = {"L3": 0.45, "L2": 0.35, "L1": 0.2, "L0": 0.0}.get(layer, 0.0)
            if not promoted:
                bonus = min(bonus, 0.2)
            lexical_score = min(0.65, len(matched) * 0.16)
            recency_score = self._recency_score(row, now=now, latest_requested=latest_requested)
            feedback_score = max(-0.2, min(0.2, float(row["priority"] or 0.0) * 0.12))
            relevance = semantic_score + lexical_score + bonus + recency_score + feedback_score
            entry = {
                "id": str(row["id"]),
                "row": row,
                "matched": matched,
                "semantic_score": semantic_score,
                "recency_score": recency_score,
                "_relevance": relevance,
                "_promoted": promoted,
                "retrieval_keys": retrieval_keys,
                "_vector": unpack(row["embedding"], int(row["embedding_dim"] or EMBEDDING_DIMENSION)),
            }
            # Byte-identical turns are one memory stored many times, not many
            # memories.  MMR is meant to suppress them but is a weighted
            # preference, so a duplicate that scores higher than everything else
            # still fills the window with itself.  Keep the best-ranked copy and
            # let the rest of the window hold different content.
            key = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
            previous = seen_content.get(key)
            if previous is None:
                seen_content[key] = entry
                ranked.append(entry)
            elif relevance > float(previous["_relevance"]):
                previous.update(entry)
        ranked.sort(key=lambda item: (-float(item["_relevance"]), str(item["id"])))
        # Accepted evidence and unreviewed candidates are separated by every
        # caller *after* retrieval, so letting them compete for the same slots
        # means a caller asking for five memories can receive five records the
        # answer surface will throw away.  That is what happened here: seven
        # candidate envelopes outranked the accepted turns holding the answer,
        # and a five-deep recall came back empty.  Fill from the accepted pool
        # first and let candidates take what is left, so candidates still
        # surface when there is little else — which is when they are useful —
        # without ever displacing evidence.
        window = max(20, int(limit) * 8)
        promoted_pool = [item for item in ranked if item.get("_promoted")]
        candidate_pool = [item for item in ranked if not item.get("_promoted")]
        selected = self._mmr_select(promoted_pool[:window], limit)
        if len(selected) < limit:
            selected += self._mmr_select(candidate_pool[:window], limit - len(selected))
        results = []
        connection = self._connect()
        try:
            for item in selected:
                row = item["row"]
                result = self._result(
                    row,
                    item["matched"],
                    item["semantic_score"],
                    recency_score=item["recency_score"],
                    related=self._related(connection, str(row["id"])),
                    retrieval_keys=item.get("retrieval_keys") or [],
                    context=self._adjacent_context(row, lookup),
                )
                result["ranking"] = {
                    "relevance": round(float(item.get("relevance", 0.0)), 6),
                    "recency": round(float(item.get("recency_score", 0.0)), 6),
                    "mmr": True,
                    "latest_query": latest_requested,
                }
                results.append(result)
        finally:
            connection.close()
        return results

    def _result(self, row: sqlite3.Row, matched: list[str], semantic_score: float = 0.0, *, recency_score: float = 0.0, related: list[dict[str, Any]] | None = None, retrieval_keys: list[str] | None = None, context: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
            # ``get()`` has always returned the role; search dropped it, so the
            # answer surface could not tell a captured user turn from an
            # assistant answer and had to guess from the text.
            "memory_role": row["role"],
            "tier": tier,
            "ref_kind": ref_kind,
            "ref_id": record_id,
            "status": status,
            "generated": generated,
            "accepted": accepted,
            "score": round(semantic_score + min(0.65, len(matched) * 0.16), 6),
            "semantic_score": round(semantic_score, 6),
            "recency_score": round(recency_score, 6),
            "retrieval_mode": "local-hybrid",
            "related": related if related is not None else [],
            "retrieval_keys": retrieval_keys if retrieval_keys is not None else [],
            "context": context if context is not None else [],
            "context_strategy": "adjacent-session-turns" if context else "none",
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
            related = self._related(connection, result_id) if row is not None else []
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
            "memory": {"session_id": row["session_id"], "message_index": row["message_index"], "role": row["role"], "content": content, "timestamp": row["timestamp"], "metadata": row["metadata"], "status": str(row["status"]), "accepted": accepted, "related": related},
            "citation": {"source": self.backend, "memory_id": result_id, "layer": layer, "evidence": content, "locator": {"layer": layer, "memory_id": result_id}, "valid": True, "generated": bool(row["generated"]), "accepted": accepted},
            "readback": {"verified": True, "status": "verified", "backend": self.backend, "id": result_id},
        }

    def observe(self, session_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Read the durable trace without ranking or generating conclusions."""

        return {"operation": "observe", **self.timeline(session_id=session_id, limit=limit)}

    def reflect(self, query: str = "", limit: int = 8, session_id: str | None = None) -> dict[str, Any]:
        """Produce a bounded, explicitly generated reflection over memory.

        This is intentionally read-only and candidate-labelled.  It gives
        hosts a Hindsight-style reflect operation without pretending that a
        rule-based digest is an accepted L2/L3 fact or requiring an LLM.
        """

        records = self.search(query, limit=max(1, min(int(limit), 20))) if query else []
        if session_id:
            records = [item for item in records if str((item.get("citation") or {}).get("provenance", {}).get("session_id") or "") == session_id]
        observations = [
            {
                "id": item.get("id"),
                "layer": item.get("memory_layer"),
                "observation": str(item.get("content") or "")[:512],
                "evidence_status": item.get("status"),
                "citation": item.get("citation"),
            }
            for item in records
        ]
        return {
            "schema_version": 1,
            "ok": True,
            "operation": "reflect",
            "backend": self.backend,
            "query": query,
            "status": "candidate" if observations else "empty",
            "generated": True,
            "accepted": False,
            "observations": observations,
            "evidence_count": len(observations),
            "limitations": ["rule-based digest", "must not be promoted without review"],
            "readback": {"verified": True, "backend": self.backend, "source_ids": [item.get("id") for item in observations]},
            "canonical_repo_changed": False,
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
