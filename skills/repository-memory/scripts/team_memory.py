#!/usr/bin/env python3
"""Shared, provider-free Team Memory store.

Team Memory is deliberately separate from repository citations and native
MemoryCore.  It stores compact, explicitly published knowledge that is useful
to more than one agent: decisions, failures, discoveries, solutions, and
handoffs.  The store is a derived user-level SQLite database; it never edits a
canonical repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from discovery import data_root
from tokenize_query import fts5_can_match, plane_terms

# ``scenario`` is the L2 record kind this system's own supervisor writes and
# the canonical exporter publishes under ``l2/accepted``.  It was missing here,
# so a hydrating store rejected a file the same pipeline had produced -- every
# host showed ``failed: 1`` on pull for the first accepted scenario.
MEMORY_TYPES = {"evidence", "decision", "discovery", "failure", "solution", "handoff", "scenario"}
STATUSES = {"candidate", "active", "superseded", "stale"}
RATINGS = {"helpful", "not_helpful", "stale", "wrong"}
SECRET_CONTENT = re.compile(
    r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{16,}|\bsk-[A-Za-z0-9_-]{16,}",
    re.IGNORECASE,
)
# Nouns that describe this store rather than anything stored in it: in Team
# Memory every record is a "memory" about a "project" for the "team", so those
# select everything.  That is a field-specific filter.  Language-level question
# scaffolding (的/是/最近/什么) lives in ``tokenize_query.STOP_TERMS`` and is
# dropped before these are consulted.
STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "what", "when", "where",
    "which", "about", "project", "memory", "team",
}
MEMORY_PAYLOAD_FIELDS = (
    "id", "memory_type", "title", "content", "summary", "scope", "provenance",
    "confidence", "status", "supersedes", "superseded_by", "valid_from",
    "valid_until", "author_agent", "reviewed_by", "activated_at", "idempotency_key", "created_at", "updated_at",
    "revision", "origin_node", "parent_revision",
)
T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _parse(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _terms(value: str) -> list[str]:
    """Tokenize a query the same way every other retrieval plane does.

    This used to be ``[\\w一-龥./:#-]{2,}`` with no segmentation, so a Chinese
    question arrived as one clause-length token that occurs verbatim nowhere and
    this store was simply unreachable in Chinese.
    """

    return plane_terms(value, STOP_WORDS)


def _default_node_id() -> str:
    configured = str(os.environ.get("REPOSITORY_MEMORY_NODE_ID") or "").strip()
    if configured:
        return configured
    identity = os.environ.get("HOSTNAME") or platform.node() or "repository-memory-node"
    return f"node-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}"


class SQLiteTeamMemoryBackend:
    """Local Team Memory adapter with lifecycle, retrieval, and sync semantics."""

    def __init__(self, path: Path | None = None, *, node_id: str | None = None):
        configured_path = str(os.environ.get("REPOSITORY_MEMORY_TEAM_DB") or "").strip()
        self.path = path or (Path(configured_path).expanduser().resolve() if configured_path else data_root() / "team-memory" / "team.sqlite3")
        self.node_id = str(node_id or _default_node_id()).strip() or "repository-memory-node"

    backend_name = "team-memory-sqlite"
    _write_attempts = 6
    _busy_timeout_ms = 5000

    @staticmethod
    def _locked(error: sqlite3.OperationalError) -> bool:
        message = str(error).casefold()
        return "locked" in message or "busy" in message

    @staticmethod
    def _expired(value: str | None, now: datetime | None = None) -> bool:
        if not value:
            return False
        try:
            expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return current >= expiry.astimezone(timezone.utc)

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT NOT NULL,
                scope TEXT NOT NULL,
                provenance TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL,
                supersedes TEXT,
                superseded_by TEXT,
                valid_from TEXT,
                valid_until TEXT,
                author_agent TEXT,
                reviewed_by TEXT,
                activated_at TEXT,
                idempotency_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                origin_node TEXT NOT NULL DEFAULT 'legacy',
                parent_revision TEXT
            )"""
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memories)")}
        for name, definition in (("reviewed_by", "TEXT"), ("activated_at", "TEXT"), ("revision", "INTEGER NOT NULL DEFAULT 1"), ("origin_node", "TEXT NOT NULL DEFAULT 'legacy'"), ("parent_revision", "TEXT")):
            if name not in columns:
                connection.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS memories_status_idx ON memories(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS memories_type_idx ON memories(memory_type)")
        connection.execute("CREATE INDEX IF NOT EXISTS memories_scope_idx ON memories(scope)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS memory_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                rating TEXT NOT NULL,
                note TEXT NOT NULL,
                agent TEXT,
                created_at TEXT NOT NULL,
                feedback_id TEXT,
                origin_node TEXT NOT NULL DEFAULT 'legacy'
            )"""
        )
        feedback_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memory_feedback)")}
        for name, definition in (("feedback_id", "TEXT"), ("origin_node", "TEXT NOT NULL DEFAULT 'legacy'")):
            if name not in feedback_columns:
                connection.execute(f"ALTER TABLE memory_feedback ADD COLUMN {name} {definition}")
        for row in connection.execute("SELECT id, memory_id, rating, note, agent, created_at FROM memory_feedback WHERE feedback_id IS NULL OR feedback_id = ''"):
            basis = "|".join(str(row[key] or "") for key in ("id", "memory_id", "rating", "note", "agent", "created_at"))
            feedback_id = f"legacy:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]}"
            connection.execute("UPDATE memory_feedback SET feedback_id = ? WHERE id = ?", (feedback_id, row["id"]))
        connection.execute("CREATE INDEX IF NOT EXISTS memory_feedback_identity_idx ON memory_feedback(memory_id, feedback_id)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS memory_revisions (
                memory_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                origin_node TEXT NOT NULL,
                parent_revision TEXT,
                payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (memory_id, revision_id)
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS memory_revisions_parent_idx ON memory_revisions(memory_id, parent_revision)")
        for row in connection.execute("SELECT * FROM memories"):
            revision_id = self._revision_id(row["revision"], row["origin_node"])
            if connection.execute("SELECT 1 FROM memory_revisions WHERE memory_id = ? AND revision_id = ?", (row["id"], revision_id)).fetchone() is None:
                self._append_revision(connection, row)
        try:
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, body)")
        except sqlite3.OperationalError:
            pass

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self._write_attempts):
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000)
                connection.row_factory = sqlite3.Row
                # WAL allows readers to continue while another agent publishes.
                # busy_timeout plus the bounded write retry below makes the
                # concurrency contract explicit instead of relying on sqlite's
                # process defaults.
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                self._ensure_schema(connection)
                connection.commit()
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
                return connection
            except sqlite3.OperationalError as error:
                last_error = error
                if connection is not None:
                    connection.close()
                if not self._locked(error) or attempt == self._write_attempts - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
        raise last_error or sqlite3.OperationalError("unable to open Team Memory database")

    def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """Run one bounded transaction for concurrent agent writers."""

        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self._write_attempts):
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                value = operation(connection)
                connection.commit()
                return value
            except sqlite3.OperationalError as error:
                last_error = error
                if connection is not None:
                    connection.rollback()
                    connection.close()
                if not self._locked(error) or attempt == self._write_attempts - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
            finally:
                if connection is not None:
                    connection.close()
        raise last_error or sqlite3.OperationalError("Team Memory write failed")
    @staticmethod
    def _fts_available(connection: sqlite3.Connection) -> bool:
        try:
            connection.execute("SELECT 1 FROM memories_fts LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    def health(self) -> dict[str, Any]:
        connection = self._connect()
        total = 0
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        feedback = 0
        revisions = 0
        expired = 0
        fts = False
        retention_details: list[dict[str, Any]] = []
        try:
            total = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            by_status = {str(row[0]): int(row[1]) for row in connection.execute("SELECT status, COUNT(*) FROM memories GROUP BY status")}
            by_type = {str(row[0]): int(row[1]) for row in connection.execute("SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type")}
            feedback = int(connection.execute("SELECT COUNT(*) FROM memory_feedback").fetchone()[0])
            revisions = int(connection.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0])
            expired = sum(1 for row in connection.execute("SELECT valid_until FROM memories WHERE status = 'active' AND valid_until IS NOT NULL") if self._expired(row[0]))
            fts = self._fts_available(connection)
            for mem_row in connection.execute("SELECT id, revision, origin_node FROM memories"):
                mem_id = mem_row["id"]
                rev_count = int(connection.execute("SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (mem_id,)).fetchone()[0])
                current_chain = 0
                cursor = self._revision_id(mem_row["revision"], mem_row["origin_node"])
                seen: set[str] = set()
                while cursor and cursor not in seen:
                    current_chain += 1
                    seen.add(cursor)
                    parent = connection.execute(
                        "SELECT parent_revision FROM memory_revisions WHERE memory_id = ? AND revision_id = ?",
                        (mem_id, cursor),
                    ).fetchone()
                    cursor = str(parent[0]) if parent and parent[0] else ""
                retention_details.append({"id": mem_id, "total_revisions": rev_count, "current_ancestor_chain": current_chain})
        finally:
            connection.close()
        retention = {item["id"]: {key: value for key, value in item.items() if key != "id"} for item in retention_details}
        return {
            "backend": self.backend_name,
            "backend_kind": "local",
            "configured": True,
            "reachable": True,
            "status": "ready",
            "path": str(self.path),
            "record_count": total,
            "by_status": by_status,
            "by_type": by_type,
            "feedback_count": feedback,
            "revision_count": revisions,
            "retention": retention,
            "retention_summary": {"total_revisions": revisions, "revision_records": retention_details},
            "active_expired": expired,
            "index": "fts5" if fts else "sqlite-scan",
            "retrieval_strategy": "keyword-only",
            "semantic_available": False,
            "canonical_repo_changed": False,
            "sync": {"export": True, "import": True, "remote_service": False},
            "node_id": self.node_id,
            "merge": {"strategy": "causal-revision", "wall_clock_lww": False},
            "concurrency": {"journal_mode": "wal", "busy_timeout_ms": self._busy_timeout_ms, "write_retry_attempts": self._write_attempts},
        }

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> tuple[str, str, str, dict[str, Any], dict[str, Any], float, str]:
        memory_type = str(payload.get("type") or payload.get("memory_type") or "").strip().lower()
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"memory type must be one of: {', '.join(sorted(MEMORY_TYPES))}")
        title = str(payload.get("title") or payload.get("summary") or memory_type).strip()
        content = str(payload.get("content") or payload.get("body") or payload.get("summary") or "").strip()
        if not content:
            raise ValueError("team memory requires content, body, or summary")
        if SECRET_CONTENT.search(title + "\n" + content):
            raise ValueError("team memory contains a secret-like value")
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        # Accept the concise input shape from the design document while keeping
        # the stored provenance explicit and machine-readable.
        for key in ("repo", "repository", "issue", "branch", "task"):
            if key in payload and key not in scope:
                scope[key] = payload[key]
        for key in ("agent", "agent_id", "session", "session_id", "commits", "citations"):
            if key in payload and key not in provenance:
                provenance[key] = payload[key]
        try:
            confidence = float(payload.get("confidence", 0.5))
        except (TypeError, ValueError):
            raise ValueError("confidence must be a number between 0 and 1") from None
        confidence = max(0.0, min(1.0, confidence))
        status = str(payload.get("status") or "candidate").strip().lower()
        if status not in STATUSES - {"superseded", "stale"}:
            raise ValueError("new team memory status must be candidate or active")
        return memory_type, title, content, scope, provenance, confidence, status

    @staticmethod
    def _id(memory_type: str, title: str, content: str, scope: dict[str, Any], idempotency: str | None) -> str:
        basis = idempotency or _json({"type": memory_type, "title": title, "content": content, "scope": scope})
        return f"team:{memory_type}:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _revision_id(revision: int | str | None, origin_node: str | None) -> str:
        return f"{origin_node or 'legacy'}:{int(revision or 1)}"

    @classmethod
    def _next_revision(cls, row: sqlite3.Row, node_id: str) -> tuple[int, str, str]:
        revision = int(row["revision"] or 1) + 1
        return revision, node_id, cls._revision_id(row["revision"], row["origin_node"])

    @staticmethod
    def _revision_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {field: row[field] for field in MEMORY_PAYLOAD_FIELDS}

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def _append_revision(self, connection: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> None:
        payload = self._revision_payload(row)
        revision_id = self._revision_id(row["revision"], row["origin_node"])
        payload_json = _json(payload)
        connection.execute(
            """INSERT OR IGNORE INTO memory_revisions
            (memory_id, revision_id, revision, origin_node, parent_revision, payload, payload_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], revision_id, int(row["revision"] or 1), row["origin_node"] or "legacy", row["parent_revision"], payload_json, self._payload_hash(payload), row["updated_at"] or _now()),
        )

    def _is_ancestor(self, connection: sqlite3.Connection, memory_id: str, ancestor: str, descendant: str) -> bool:
        """Return whether ``ancestor`` is in the retained causal chain."""

        cursor = descendant
        seen: set[str] = set()
        while cursor and cursor not in seen:
            if cursor == ancestor:
                return True
            seen.add(cursor)
            row = connection.execute(
                "SELECT parent_revision FROM memory_revisions WHERE memory_id = ? AND revision_id = ?",
                (memory_id, cursor),
            ).fetchone()
            cursor = str(row[0]) if row and row[0] else ""
        return False

    def _row(self, row: sqlite3.Row, feedback: list[sqlite3.Row] | None = None) -> dict[str, Any]:
        scope = _parse(row["scope"], {})
        provenance = _parse(row["provenance"], {})
        feedback_rows = feedback or []
        helpful = sum(1 for item in feedback_rows if item["rating"] == "helpful")
        negative = sum(1 for item in feedback_rows if item["rating"] in {"wrong", "stale", "not_helpful"})
        evidence_backed = bool(provenance.get("citations") or provenance.get("commits"))
        evidence_status = "evidence-backed" if evidence_backed else "experience-backed"
        expired = self._expired(row["valid_until"])
        if row["status"] == "candidate":
            evidence_status = "candidate"
        elif row["status"] in {"stale", "superseded"}:
            evidence_status = row["status"]
        elif expired:
            evidence_status = "expired"
        return {
            "id": row["id"],
            "kind": row["memory_type"],
            "memory_type": row["memory_type"],
            "title": row["title"],
            "summary": row["summary"],
            "content": row["content"],
            "source": "team-memory",
            "repository": scope.get("repo") or scope.get("repository"),
            "scope": scope,
            "provenance": provenance,
            "author_agent": row["author_agent"],
            "reviewed_by": row["reviewed_by"],
            "activated_at": row["activated_at"],
            "confidence": float(row["confidence"]),
            "status": row["status"],
            "evidence_status": evidence_status,
            "generated": False,
            "supersedes": row["supersedes"],
            "superseded_by": row["superseded_by"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "expired": expired,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": int(row["revision"] or 1),
            "origin_node": row["origin_node"] or "legacy",
            "revision_id": self._revision_id(row["revision"], row["origin_node"]),
            "parent_revision": row["parent_revision"],
            "reuse": {"helpful": helpful, "negative": negative, "total": helpful + negative},
            "citation": {
                "source": "team-memory",
                "memory_id": row["id"],
                "repository": scope.get("repo") or scope.get("repository"),
                "commit": (provenance.get("commits") or [None])[0] if isinstance(provenance.get("commits"), list) else provenance.get("commit"),
                "path": (provenance.get("citations") or [None])[0] if isinstance(provenance.get("citations"), list) else None,
                "valid": evidence_backed,
                "evidence_status": evidence_status,
            },
        }

    def publish(self, payload: dict[str, Any], *, default_status: str = "candidate") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("memory_publish requires a JSON object")
        memory_type, title, content, scope, provenance, confidence, status = self._validate_payload({**payload, "status": payload.get("status") or default_status})
        summary = str(payload.get("summary") or content[:400]).strip()
        now = _now()
        idempotency = str(payload.get("idempotency_key") or payload.get("idempotency") or "").strip() or None
        memory_id = str(payload.get("id") or self._id(memory_type, title, content, scope, idempotency))
        if not memory_id.startswith("team:"):
            memory_id = "team:" + memory_id
        author_agent = str(payload.get("author_agent") or provenance.get("agent") or provenance.get("agent_id") or "").strip() or None
        reviewed_by = str(payload.get("reviewed_by") or "").strip() or None
        activated_at = str(payload.get("activated_at") or payload.get("accepted_at") or "").strip() or None
        supersedes = str(payload.get("supersedes") or "").strip() or None
        valid_from = str(payload.get("valid_from") or now)
        valid_until = str(payload.get("valid_until") or "") or None
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            lineage_duplicate = False
            existing = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if existing is None and idempotency:
                # ``idempotency_key`` is UNIQUE, so a row published under an
                # earlier id scheme still owns this key.  Matching on the id
                # alone would miss it and turn a re-publish into an
                # IntegrityError instead of the documented duplicate receipt.
                existing = connection.execute("SELECT * FROM memories WHERE idempotency_key = ?", (idempotency,)).fetchone()
            source_memory_id = str(provenance.get("source_memory_id") or "").strip()
            central_id = str(provenance.get("central_id") or "").strip()
            if existing is None and source_memory_id and central_id:
                normalized_source = source_memory_id if source_memory_id.startswith("team:") else "team:" + source_memory_id
                source_row = connection.execute("SELECT * FROM memories WHERE id = ?", (normalized_source,)).fetchone()
                if source_row is not None:
                    source_scope = json.loads(source_row["scope"] or "{}")
                    same_identity = (
                        source_row["memory_type"] == memory_type
                        and str(source_row["content"] or "").strip() == content.strip()
                        and source_scope == scope
                    )
                    if same_identity:
                        existing = source_row
                        lineage_duplicate = True
            if existing:
                # A reviewed canonical wrapper carries lifecycle information
                # back to its source row.  Identity was checked above, so this
                # updates one memory rather than creating a second projection.
                if lineage_duplicate and status == "active" and existing["status"] != "active":
                    revision, origin_node, parent = self._next_revision(existing, self.node_id)
                    connection.execute(
                        "UPDATE memories SET status = 'active', reviewed_by = ?, activated_at = ?, updated_at = ?, revision = ?, origin_node = ?, parent_revision = ? WHERE id = ?",
                        (reviewed_by, activated_at or now, now, revision, origin_node, parent, existing["id"]),
                    )
                    existing = connection.execute("SELECT * FROM memories WHERE id = ?", (existing["id"],)).fetchone()
                    self._append_revision(connection, existing)
                return {"schema_version": 1, "ok": True, "duplicate": True, "memory": self._row(existing), "canonical_repo_changed": False}
            superseded_row = connection.execute("SELECT * FROM memories WHERE id = ?", (supersedes,)).fetchone() if supersedes else None
            parent_revision = self._revision_id(superseded_row["revision"], superseded_row["origin_node"]) if superseded_row else None
            connection.execute(
                """INSERT INTO memories
                (id, memory_type, title, content, summary, scope, provenance, confidence, status,
                 supersedes, superseded_by, valid_from, valid_until, author_agent, reviewed_by, activated_at, idempotency_key,
                created_at, updated_at, revision, origin_node, parent_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (memory_id, memory_type, title, content, summary, _json(scope), _json(provenance), confidence, status, supersedes, valid_from, valid_until, author_agent, reviewed_by, activated_at, idempotency, now, now, 1, self.node_id, parent_revision),
            )
            if superseded_row and superseded_row["status"] != "superseded":
                revision, origin_node, parent = self._next_revision(superseded_row, self.node_id)
                connection.execute("UPDATE memories SET status = 'superseded', superseded_by = ?, updated_at = ?, revision = ?, origin_node = ?, parent_revision = ? WHERE id = ?", (memory_id, now, revision, origin_node, parent, supersedes))
                self._append_revision(connection, connection.execute("SELECT * FROM memories WHERE id = ?", (supersedes,)).fetchone())
            if self._fts_available(connection):
                body = "\n".join((title, summary, content, _json(scope), _json(provenance)))
                connection.execute("INSERT INTO memories_fts (id, body) VALUES (?, ?)", (memory_id, body))
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            self._append_revision(connection, row)
            return {"schema_version": 1, "ok": True, "duplicate": False, "memory": self._row(row), "write_operation": "explicit", "canonical_repo_changed": False}
        return self._write(operation)

    def session_representative(self, *, author_agent: str | None, session: str | None, memory_type: str) -> dict[str, Any] | None:
        """Return the un-reviewed candidate that stands for one session.

        Autocapture emits one candidate per assistant turn, so a single
        conversation can deposit a dozen rows about one thing: measured across
        one 42-minute thread, pairwise token-set overlap ran 0.19-0.34, below
        the 0.72 scored by two genuinely different answers.  Text similarity
        cannot separate a storm from real work, so the grouping comes from
        provenance instead -- one (agent, session, kind) is one thing.

        Which row stands for the group is a separate question, and recency
        answers it wrong.  Measured over the seven real storms in this store,
        candidates arrive longest-first and decay into wrap-up remarks: one
        session ran 2732 chars, then 100, 123, 56, and closed on 65.  Keeping
        the newest retired the substantive row in four of the seven; keeping
        the longest kept it in all seven.  So the fullest statement stands and
        ties fall back to the newest.

        Only candidates are considered.  Superseding a reviewed record would
        silently retract knowledge a human already accepted, and a record
        without a session carries no grouping signal at all, so both are left
        alone.
        """

        if not session or not author_agent:
            return None
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, content, provenance FROM memories WHERE status = 'candidate' AND author_agent = ? AND memory_type = ? ORDER BY created_at",
                (str(author_agent), memory_type),
            ).fetchall()
        finally:
            connection.close()
        best: dict[str, Any] | None = None
        for row in rows:
            provenance = _parse(row["provenance"], {})
            if str(provenance.get("session") or provenance.get("session_id") or "") != str(session):
                continue
            length = len(str(row["content"] or ""))
            # `>=` walks the created_at ordering, so an equal-length tie leaves
            # the newest holding the slot.
            if best is None or length >= int(best["length"]):
                best = {"id": str(row["id"]), "length": length}
        return best

    def collapse_session_candidates(self, *, apply: bool = False) -> dict[str, Any]:
        """Retire the per-turn pile-up one conversation leaves behind.

        Capture predating the session-scoped supersede in the autocapture path
        recorded every assistant turn as its own candidate, so a single thread
        could deposit twenty rows about one thing.  This applies the same rule
        retroactively: within one (agent, session, kind) the fullest candidate
        stands and the rest are superseded by it.  See
        `session_representative` for why the fullest rather than the newest.

        Defaults to a dry run.  Reviewed records and candidates with no session
        are never touched, and superseded rows keep their content and revision
        history -- this retires rows, it does not delete them.
        """

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
            no_session = 0
            for row in connection.execute("SELECT * FROM memories WHERE status = 'candidate' ORDER BY created_at").fetchall():
                provenance = _parse(row["provenance"], {})
                session = str(provenance.get("session") or provenance.get("session_id") or "")
                agent = str(row["author_agent"] or "")
                if not session or not agent:
                    no_session += 1
                    continue
                groups.setdefault((agent, session, str(row["memory_type"])), []).append(row)

            now = _now()
            collapsed: list[dict[str, Any]] = []
            for (agent, session, memory_type), rows in groups.items():
                if len(rows) < 2:
                    continue
                # `rows` is in created_at order, so max() on length alone
                # leaves the earliest of an equal-length tie holding the slot;
                # the index term flips that back to the newest.
                winner = max(enumerate(rows), key=lambda pair: (len(str(pair[1]["content"] or "")), pair[0]))[1]
                for row in rows:
                    if row["id"] == winner["id"]:
                        continue
                    collapsed.append({"id": str(row["id"]), "agent": agent, "session": session, "memory_type": memory_type, "superseded_by": str(winner["id"])})
                    if not apply:
                        continue
                    revision, origin_node, parent = self._next_revision(row, self.node_id)
                    connection.execute(
                        "UPDATE memories SET status = 'superseded', superseded_by = ?, updated_at = ?, revision = ?, origin_node = ?, parent_revision = ? WHERE id = ?",
                        (str(winner["id"]), now, revision, origin_node, parent, row["id"]),
                    )
                    self._append_revision(connection, connection.execute("SELECT * FROM memories WHERE id = ?", (row["id"],)).fetchone())
            remaining = int(connection.execute("SELECT COUNT(*) FROM memories WHERE status = 'candidate'").fetchone()[0])
            return {
                "schema_version": 1,
                "ok": True,
                "applied": bool(apply),
                "collapsed": len(collapsed),
                "records": collapsed,
                "candidates_without_session": no_session,
                "candidates_remaining": remaining if apply else remaining - len(collapsed),
                "canonical_repo_changed": False,
            }

        return self._write(operation)

    def activate(self, memory_id: str, reviewer: str | None = None) -> dict[str, Any]:
        """Promote one candidate after explicit human/agent review."""

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(f"team memory not found: {memory_id}")
            if row["status"] == "active":
                return {"schema_version": 1, "ok": True, "memory_id": memory_id, "status": "active", "duplicate": True, "canonical_repo_changed": False}
            if row["status"] != "candidate":
                raise ValueError(f"only candidate Team Memory can be activated: {memory_id}")
            now = _now()
            def transition(target: sqlite3.Row) -> None:
                revision, origin_node, parent = self._next_revision(target, self.node_id)
                connection.execute("UPDATE memories SET status = 'active', updated_at = ?, revision = ?, origin_node = ?, parent_revision = ?, reviewed_by = COALESCE(?, reviewed_by), activated_at = ? WHERE id = ?", (now, revision, origin_node, parent, reviewer, now, target["id"]))
                self._append_revision(connection, connection.execute("SELECT * FROM memories WHERE id = ?", (target["id"],)).fetchone())
            transition(row)
            # One memory can sit in the store as two projections: the local
            # original and a central wrapper hydrated from the canonical
            # repository, linked by ``provenance.source_memory_id``.  The
            # review activated the *memory*, so every projection transitions
            # together -- otherwise the exporter, which prefers the original
            # for its richer provenance, keeps publishing ``candidate`` and
            # the activation never reaches any other agent.  Measured before
            # this change: 71 activations moved 3 files.
            try:
                own_source = str((json.loads(row["provenance"] or "{}")).get("source_memory_id") or "")
            except json.JSONDecodeError:
                own_source = ""
            activated_siblings: list[str] = []
            for other in connection.execute("SELECT * FROM memories WHERE status = 'candidate'").fetchall():
                if other["id"] == memory_id:
                    continue
                try:
                    other_source = str((json.loads(other["provenance"] or "{}")).get("source_memory_id") or "")
                except json.JSONDecodeError:
                    continue
                if other_source == memory_id or (own_source and own_source == other["id"]):
                    transition(other)
                    activated_siblings.append(str(other["id"]))
            updated = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return {"schema_version": 1, "ok": True, "memory_id": memory_id, "status": "active", "duplicate": False, "reviewer": reviewer, "memory": self._row(updated), "activated_siblings": activated_siblings, "canonical_repo_changed": False}

        return self._write(operation)

    def get(self, memory_id: str, *, include_feedback: bool = True) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(f"team memory not found: {memory_id}")
            feedback = connection.execute("SELECT rating, note, agent, created_at, feedback_id, origin_node FROM memory_feedback WHERE memory_id = ? ORDER BY id", (memory_id,)).fetchall() if include_feedback else []
        finally:
            connection.close()
        value = self._row(row, feedback)
        if include_feedback:
            value["feedback"] = [dict(item) for item in feedback]
        return {"schema_version": 1, "found": True, "id": memory_id, "source": "team-memory", "result": value, "canonical_repo_changed": False}

    def search(self, query: str, *, limit: int = 10, repo: str | None = None, issue: str | None = None, branch: str | None = None, agent: str | None = None, include_candidates: bool = True) -> dict[str, Any]:
        terms = _terms(query)
        connection = self._connect()
        try:
            # FTS is only a candidate pre-filter; the real match is the
            # substring pass below, which is CJK-safe.  Scan instead of
            # pre-filtering when the index cannot see a term — see
            # ``tokenize_query.fts5_can_match``.
            prefilter = bool(terms) and all(fts5_can_match(term) for term in terms)
            if self._fts_available(connection) and prefilter:
                match = " OR ".join('"' + term.replace('"', "") + '"' for term in terms)
                rows = connection.execute("SELECT m.* FROM memories m JOIN memories_fts f ON f.id = m.id WHERE memories_fts MATCH ?", (match,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM memories").fetchall()
            feedback_rows = connection.execute("SELECT memory_id, rating FROM memory_feedback").fetchall()
        finally:
            connection.close()
        feedback_map: dict[str, list[sqlite3.Row]] = {}
        for item in feedback_rows:
            feedback_map.setdefault(str(item["memory_id"]), []).append(item)
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        expired_count = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            if row["status"] == "superseded" or row["status"] == "stale":
                continue
            if self._expired(row["valid_until"], now):
                expired_count += 1
                continue
            if row["status"] == "candidate" and not include_candidates:
                continue
            scope = _parse(row["scope"], {})
            if repo and repo.casefold() not in str(scope.get("repo") or scope.get("repository") or "").casefold():
                continue
            if issue and issue.casefold() not in str(scope.get("issue") or "").casefold():
                continue
            if branch and branch.casefold() not in str(scope.get("branch") or "").casefold():
                continue
            provenance = _parse(row["provenance"], {})
            if agent and agent.casefold() not in str(row["author_agent"] or provenance.get("agent") or provenance.get("agent_id") or "").casefold():
                continue
            body = " ".join((row["title"], row["summary"], row["content"], _json(scope), _json(provenance))).casefold()
            matched = [term for term in terms if term in body]
            if terms and not matched:
                continue
            feedback = feedback_map.get(str(row["id"]), [])
            helpful = sum(1 for item in feedback if item["rating"] == "helpful")
            negative = sum(1 for item in feedback if item["rating"] in {"wrong", "stale", "not_helpful"})
            score = (len(matched) * 2.0) + float(row["confidence"]) + min(helpful, 10) * 0.15 - min(negative, 10) * 0.2
            if row["status"] == "active":
                score += 0.4
            ranked.append((score, str(row["id"]), self._row(row, feedback)))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        active = [item for score, _id, item in ranked if item["status"] == "active"][:limit]
        candidates = [item for score, _id, item in ranked if item["status"] == "candidate"][:limit]
        return {
            "schema_version": 1,
            "query": query,
            "retrieval_mode": "lexical",
            "semantic_available": False,
            "active": active,
            "candidates": candidates,
            "abstain": not active,
            "diagnostics": {"result_count": len(active), "candidate_count": len(candidates), "expired_count": expired_count, "query_terms": terms, "filters": {"repo": repo, "issue": issue, "branch": branch, "agent": agent}},
        }

    def feedback(self, memory_id: str, rating: str, note: str = "", agent: str | None = None, feedback_id: str | None = None) -> dict[str, Any]:
        rating = str(rating).strip().lower()
        if rating not in RATINGS:
            raise ValueError(f"rating must be one of: {', '.join(sorted(RATINGS))}")
        note = str(note)[:2000]
        feedback_id = str(feedback_id or "").strip()
        if not feedback_id:
            basis = "|".join((memory_id, rating, note, str(agent or "")))
            feedback_id = f"{self.node_id}:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]}"
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if connection.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
                raise KeyError(f"team memory not found: {memory_id}")
            duplicate = connection.execute("SELECT 1 FROM memory_feedback WHERE memory_id = ? AND feedback_id = ?", (memory_id, feedback_id)).fetchone()
            if duplicate:
                status_row = connection.execute("SELECT status FROM memories WHERE id = ?", (memory_id,)).fetchone()
                return {"schema_version": 1, "ok": True, "memory_id": memory_id, "feedback_id": feedback_id, "rating": rating, "status": status_row[0] if status_row else None, "duplicate": True, "canonical_repo_changed": False}
            created_at = _now()
            connection.execute("INSERT INTO memory_feedback (memory_id, rating, note, agent, created_at, feedback_id, origin_node) VALUES (?, ?, ?, ?, ?, ?, ?)", (memory_id, rating, note, agent, created_at, feedback_id, self.node_id))
            transition = None
            if rating in {"stale", "wrong"}:
                now = _now()
                current = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
                revision, origin_node, parent = self._next_revision(current, self.node_id)
                connection.execute("UPDATE memories SET confidence = MAX(0, confidence - 0.1), updated_at = ?, revision = ?, origin_node = ?, parent_revision = ? WHERE id = ?", (now, revision, origin_node, parent, memory_id))
                if rating == "wrong":
                    connection.execute("UPDATE memories SET status = 'stale' WHERE id = ? AND status NOT IN ('superseded', 'stale')", (memory_id,))
                    transition = "stale"
                else:
                    agents = {str(row[0]).strip() for row in connection.execute("SELECT DISTINCT agent FROM memory_feedback WHERE memory_id = ? AND rating = 'stale' AND agent IS NOT NULL AND TRIM(agent) != ''", (memory_id,))}
                    if len(agents) >= 2:
                        connection.execute("UPDATE memories SET status = 'stale' WHERE id = ? AND status = 'active'", (memory_id,))
                        transition = "stale"
                self._append_revision(connection, connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone())
            status_row = connection.execute("SELECT status FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return {"schema_version": 1, "ok": True, "memory_id": memory_id, "feedback_id": feedback_id, "rating": rating, "status": status_row[0] if status_row else None, "lifecycle_transition": transition, "duplicate": False, "canonical_repo_changed": False}
        return self._write(operation)

    def export_bundle(self) -> dict[str, Any]:
        """Return a portable, mergeable Team Memory bundle.

        The bundle is intentionally plain JSON so it can move through rsync,
        an artifact store, or a human-reviewed handoff without introducing a
        network service or writing into a canonical repository.
        """

        connection = self._connect()
        try:
            records = [dict(row) for row in connection.execute("SELECT * FROM memories ORDER BY id").fetchall()]
            feedback = [dict(row) for row in connection.execute("SELECT memory_id, rating, note, agent, created_at, feedback_id, origin_node FROM memory_feedback ORDER BY memory_id, created_at, id").fetchall()]
            revisions = [dict(row) for row in connection.execute("SELECT memory_id, revision_id, revision, origin_node, parent_revision, payload, payload_hash, created_at FROM memory_revisions ORDER BY memory_id, revision, revision_id").fetchall()]
        finally:
            connection.close()
        return {"schema_version": 3, "kind": "repository-memory-team-bundle", "backend": self.backend_name, "node_id": self.node_id, "exported_at": _now(), "records": records, "revisions": revisions, "feedback": feedback}

    def import_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(bundle, dict) or bundle.get("kind") != "repository-memory-team-bundle":
            raise ValueError("invalid Team Memory bundle")
        if int(bundle.get("schema_version", 1)) not in {1, 2, 3}:
            raise ValueError(f"unsupported Team Memory bundle schema: {bundle.get('schema_version')}")
        records = bundle.get("records") if isinstance(bundle.get("records"), list) else []
        revisions = bundle.get("revisions") if isinstance(bundle.get("revisions"), list) else []
        feedback = bundle.get("feedback") if isinstance(bundle.get("feedback"), list) else []

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            inserted = updated = skipped = conflicts = stale_ignored = feedback_added = feedback_replayed = revisions_inserted = revisions_skipped = 0
            conflict_records: list[dict[str, Any]] = []
            incoming_confidence: dict[str, float] = {}
            mutable_fields = ("memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "supersedes", "superseded_by", "valid_from", "valid_until", "author_agent", "reviewed_by", "activated_at")
            for incoming in revisions:
                if not isinstance(incoming, dict) or not str(incoming.get("memory_id") or "").startswith("team:"):
                    raise ValueError("Team Memory bundle contains an invalid revision")
                revision = int(incoming.get("revision") or 1)
                origin_node = str(incoming.get("origin_node") or "legacy")
                revision_id = str(incoming.get("revision_id") or self._revision_id(revision, origin_node))
                payload = incoming.get("payload")
                if not isinstance(payload, str):
                    payload = _json(payload if isinstance(payload, dict) else {})
                payload_hash = str(incoming.get("payload_hash") or self._payload_hash(_parse(payload, {})))
                current_revision = connection.execute("SELECT payload_hash FROM memory_revisions WHERE memory_id = ? AND revision_id = ?", (incoming["memory_id"], revision_id)).fetchone()
                if current_revision:
                    if str(current_revision[0]) != payload_hash:
                        conflicts += 1
                        conflict_records.append({"id": incoming["memory_id"], "revision_id": revision_id, "reason": "revision id already exists with different payload"})
                    else:
                        revisions_skipped += 1
                    continue
                parent_revision = str(incoming.get("parent_revision") or "").strip() or None
                if parent_revision and connection.execute(
                    "SELECT 1 FROM memory_revisions WHERE memory_id = ? AND revision_id = ?",
                    (incoming["memory_id"], parent_revision),
                ).fetchone() is None:
                    conflicts += 1
                    conflict_records.append({
                        "id": incoming["memory_id"],
                        "revision_id": revision_id,
                        "reason": "parent_revision not found locally; causal history is incomplete",
                        "missing_parent": parent_revision,
                    })
                    continue
                connection.execute(
                    """INSERT INTO memory_revisions (memory_id, revision_id, revision, origin_node, parent_revision, payload, payload_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (incoming["memory_id"], revision_id, revision, origin_node, parent_revision, payload, payload_hash, incoming.get("created_at") or _now()),
                )
                revisions_inserted += 1
            for incoming in records:
                if not isinstance(incoming, dict) or not str(incoming.get("id") or "").startswith("team:"):
                    raise ValueError("Team Memory bundle contains an invalid record")
                record = {key: incoming.get(key) for key in ("id", "memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "supersedes", "superseded_by", "valid_from", "valid_until", "author_agent", "reviewed_by", "activated_at", "idempotency_key", "created_at", "updated_at", "revision", "origin_node", "parent_revision")}
                try:
                    record["revision"] = int(record.get("revision") or 1)
                except (TypeError, ValueError):
                    raise ValueError(f"Team Memory record has invalid revision: {record.get('id')}") from None
                record["origin_node"] = str(record.get("origin_node") or "legacy")
                if not all(record.get(key) is not None for key in ("id", "memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "created_at", "updated_at")):
                    raise ValueError(f"Team Memory record is incomplete: {record.get('id')}")
                if record["memory_type"] not in MEMORY_TYPES or record["status"] not in STATUSES:
                    raise ValueError(f"Team Memory record has an unsupported type/status: {record.get('id')}")
                if SECRET_CONTENT.search(f"{record['title']}\n{record['content']}"):
                    raise ValueError(f"Team Memory record contains a secret-like value: {record.get('id')}")
                record_parent = str(record.get("parent_revision") or "").strip() or None
                if record_parent and connection.execute(
                    "SELECT 1 FROM memory_revisions WHERE memory_id = ? AND revision_id = ?",
                    (record["id"], record_parent),
                ).fetchone() is None:
                    conflicts += 1
                    conflict_records.append({
                        "id": record["id"],
                        "reason": "parent_revision not found locally; causal history is incomplete",
                        "missing_parent": record_parent,
                    })
                    continue
                existing = connection.execute("SELECT * FROM memories WHERE id = ?", (record["id"],)).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO memories (id, memory_type, title, content, summary, scope, provenance, confidence, status, supersedes, superseded_by, valid_from, valid_until, author_agent, reviewed_by, activated_at, idempotency_key, created_at, updated_at, revision, origin_node, parent_revision)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        tuple(record[key] for key in ("id", "memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "supersedes", "superseded_by", "valid_from", "valid_until", "author_agent", "reviewed_by", "activated_at", "idempotency_key", "created_at", "updated_at", "revision", "origin_node", "parent_revision")),
                    )
                    if self._fts_available(connection):
                        body = "\n".join((str(record["title"]), str(record["summary"]), str(record["content"]), str(record["scope"]), str(record["provenance"])))
                        connection.execute("INSERT OR REPLACE INTO memories_fts (id, body) VALUES (?, ?)", (record["id"], body))
                    self._append_revision(connection, connection.execute("SELECT * FROM memories WHERE id = ?", (record["id"],)).fetchone())
                    incoming_confidence[record["id"]] = float(record["confidence"])
                    inserted += 1
                else:
                    current_revision = int(existing["revision"] or 1)
                    current_origin = str(existing["origin_node"] or "legacy")
                    incoming_revision_id = self._revision_id(record["revision"], record["origin_node"])
                    current_revision_id = self._revision_id(current_revision, current_origin)
                    same_content = all(existing[field] == record[field] for field in mutable_fields)
                    if record["revision"] == current_revision and record["origin_node"] == current_origin and same_content:
                        incoming_confidence[record["id"]] = float(record["confidence"])
                        skipped += 1
                        continue
                    if record["revision"] < current_revision:
                        stale_ignored += 1
                        continue
                    if record["revision"] == current_revision:
                        if same_content:
                            incoming_confidence[record["id"]] = float(record["confidence"])
                            skipped += 1
                            continue
                        conflicts += 1
                        conflict_records.append({"id": record["id"], "reason": "concurrent revisions have the same logical version but different content", "local_revision": current_revision_id, "incoming_revision": incoming_revision_id})
                        continue
                    if not self._is_ancestor(connection, record["id"], current_revision_id, incoming_revision_id):
                        conflicts += 1
                        conflict_records.append({"id": record["id"], "reason": "incoming revision is not causally based on local revision", "local_revision": current_revision_id, "incoming_revision": incoming_revision_id, "incoming_parent": record.get("parent_revision")})
                        continue
                    connection.execute(
                        """UPDATE memories SET memory_type=?, title=?, content=?, summary=?, scope=?, provenance=?, confidence=?, status=?, supersedes=?, superseded_by=?, valid_from=?, valid_until=?, author_agent=?, reviewed_by=?, activated_at=?, idempotency_key=?, created_at=?, updated_at=?, revision=?, origin_node=?, parent_revision=? WHERE id=?""",
                        tuple(record[key] for key in ("memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "supersedes", "superseded_by", "valid_from", "valid_until", "author_agent", "reviewed_by", "activated_at", "idempotency_key", "created_at", "updated_at", "revision", "origin_node", "parent_revision")) + (record["id"],),
                    )
                    if self._fts_available(connection):
                        connection.execute("DELETE FROM memories_fts WHERE id = ?", (record["id"],))
                        body = "\n".join((str(record["title"]), str(record["summary"]), str(record["content"]), str(record["scope"]), str(record["provenance"])))
                        connection.execute("INSERT INTO memories_fts (id, body) VALUES (?, ?)", (record["id"], body))
                    self._append_revision(connection, connection.execute("SELECT * FROM memories WHERE id = ?", (record["id"],)).fetchone())
                    incoming_confidence[record["id"]] = float(record["confidence"])
                    updated += 1
            for item in feedback:
                if not isinstance(item, dict) or not item.get("memory_id"):
                    continue
                feedback_id = str(item.get("feedback_id") or "").strip()
                if not feedback_id:
                    basis = "|".join(str(item.get(key) or "") for key in ("memory_id", "rating", "note", "agent", "created_at"))
                    feedback_id = f"legacy:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]}"
                exists = connection.execute("SELECT 1 FROM memory_feedback WHERE memory_id = ? AND feedback_id = ?", (item.get("memory_id"), feedback_id)).fetchone()
                if exists:
                    continue
                connection.execute("INSERT INTO memory_feedback (memory_id, rating, note, agent, created_at, feedback_id, origin_node) VALUES (?, ?, ?, ?, ?, ?, ?)", (item.get("memory_id"), item.get("rating"), item.get("note") or "", item.get("agent"), item.get("created_at") or _now(), feedback_id, item.get("origin_node") or "legacy"))
                feedback_added += 1
                # Replay feedback transition: update confidence/status per local feedback() rules.
                # Revision is NOT bumped here — the imported record carries the correct
                # final revision from the source node so the ancestor chain stays valid.
                memory_id = str(item.get("memory_id"))
                rating = str(item.get("rating") or "").strip().lower()
                if rating in {"stale", "wrong"} and memory_id:
                    if connection.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone():
                        if memory_id in incoming_confidence:
                            # The bundled record already contains the source
                            # node's aggregate confidence.  Use it as a floor
                            # so importing the same feedback does not subtract
                            # twice, while locally existing feedback can still
                            # trigger the status transition below.
                            connection.execute("UPDATE memories SET confidence = MIN(confidence, ?) WHERE id = ?", (incoming_confidence[memory_id], memory_id))
                        else:
                            connection.execute("UPDATE memories SET confidence = MAX(0, confidence - 0.1) WHERE id = ?", (memory_id,))
                        if rating == "wrong":
                            connection.execute("UPDATE memories SET status = 'stale' WHERE id = ? AND status NOT IN ('superseded', 'stale')", (memory_id,))
                        else:
                            agent_rows = connection.execute("SELECT DISTINCT agent FROM memory_feedback WHERE memory_id = ? AND rating = 'stale' AND agent IS NOT NULL AND TRIM(agent) != ''", (memory_id,)).fetchall()
                            agents = {str(row[0]).strip() for row in agent_rows}
                            if len(agents) >= 2:
                                connection.execute("UPDATE memories SET status = 'stale' WHERE id = ? AND status = 'active'", (memory_id,))
                        feedback_replayed += 1
            return {"inserted": inserted, "updated": updated, "skipped": skipped, "stale_ignored": stale_ignored, "conflicts": conflicts, "conflict_records": conflict_records, "feedback_added": feedback_added, "feedback_replayed": feedback_replayed, "revisions_inserted": revisions_inserted, "revisions_skipped": revisions_skipped}

        result = self._write(operation)
        return {"schema_version": 3, "ok": True, "imported": result, "canonical_repo_changed": False}



    def compact(self, keep: int = 1) -> dict[str, Any]:
        """Explicitly purge unprotected historical revisions per memory.

        The current record and every revision in its retained ancestor chain
        are never deleted. Revision-log parent links remain immutable: if a
        later import needs a purged ancestor, causal validation reports a
        conflict instead of silently accepting an unverifiable update.
        """

        if keep < 1:
            raise ValueError(f"keep must be >= 1, got {keep}")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            protected: set[tuple[str, str]] = set()
            for row in connection.execute("SELECT id, revision, origin_node FROM memories"):
                memory_id = str(row["id"])
                cursor = self._revision_id(row["revision"], row["origin_node"])
                seen: set[str] = set()
                while cursor and cursor not in seen:
                    protected.add((memory_id, cursor))
                    seen.add(cursor)
                    parent = connection.execute(
                        "SELECT parent_revision FROM memory_revisions WHERE memory_id = ? AND revision_id = ?",
                        (memory_id, cursor),
                    ).fetchone()
                    cursor = str(parent[0]) if parent and parent[0] else ""

            rows = connection.execute(
                "SELECT memory_id, revision_id, revision FROM memory_revisions ORDER BY memory_id, revision DESC, revision_id DESC"
            ).fetchall()
            unprotected_seen: dict[str, int] = {}
            purge: list[tuple[str, str]] = []
            for row in rows:
                key = (str(row["memory_id"]), str(row["revision_id"]))
                if key in protected:
                    continue
                memory_id = key[0]
                unprotected_seen[memory_id] = unprotected_seen.get(memory_id, 0) + 1
                if unprotected_seen[memory_id] <= keep:
                    continue
                purge.append(key)
            for memory_id, revision_id in purge:
                connection.execute(
                    "DELETE FROM memory_revisions WHERE memory_id = ? AND revision_id = ?",
                    (memory_id, revision_id),
                )

            remaining = int(
                connection.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]
            )
            return {
                "purged": len(purge),
                "protected_ancestors": len(protected),
                "remaining_revisions": remaining,
                "keep": keep,
            }

        result = self._write(operation)
        result["schema_version"] = 3
        result["ok"] = True
        result["canonical_repo_changed"] = False
        return result


TeamMemoryStore = SQLiteTeamMemoryBackend


class TeamMemoryBackend(Protocol):
    """Stable seam used by the runtime; concrete storage stays behind it."""

    def health(self) -> dict[str, Any]: ...
    def publish(self, payload: dict[str, Any], *, default_status: str = "candidate") -> dict[str, Any]: ...
    def session_representative(self, *, author_agent: str | None, session: str | None, memory_type: str) -> dict[str, Any] | None: ...
    def collapse_session_candidates(self, *, apply: bool = False) -> dict[str, Any]: ...
    def activate(self, memory_id: str, reviewer: str | None = None) -> dict[str, Any]: ...
    def get(self, memory_id: str, *, include_feedback: bool = True) -> dict[str, Any]: ...
    def search(self, query: str, *, limit: int = 10, repo: str | None = None, issue: str | None = None, branch: str | None = None, agent: str | None = None, include_candidates: bool = True) -> dict[str, Any]: ...
    def feedback(self, memory_id: str, rating: str, note: str = "", agent: str | None = None, feedback_id: str | None = None) -> dict[str, Any]: ...
    def export_bundle(self) -> dict[str, Any]: ...
    def import_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]: ...


def team_memory_backend() -> TeamMemoryBackend:
    backend = str(os.environ.get("REPOSITORY_MEMORY_TEAM_BACKEND") or "sqlite").strip().lower()
    if backend not in {"sqlite", ""}:
        raise RuntimeError(f"unsupported Team Memory backend '{backend}'; supported backend: sqlite")
    return SQLiteTeamMemoryBackend()


def team_memory_store() -> TeamMemoryStore:
    return team_memory_backend()  # type: ignore[return-value]
