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
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from discovery import data_root

MEMORY_TYPES = {"evidence", "decision", "discovery", "failure", "solution", "handoff"}
STATUSES = {"candidate", "active", "superseded", "stale"}
RATINGS = {"helpful", "not_helpful", "stale", "wrong"}
SECRET_CONTENT = re.compile(
    r"-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/.+=]{16,}|\bsk-[A-Za-z0-9_-]{16,}",
    re.IGNORECASE,
)
STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "what", "when", "where",
    "which", "about", "project", "memory", "team", "最近", "最近的", "之前", "怎么", "什么",
}
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
    raw = re.findall(r"[\w一-龥./:#-]{2,}", value or "", re.UNICODE)
    return [term.casefold() for term in dict.fromkeys(raw) if term.casefold() not in STOP_WORDS]


class SQLiteTeamMemoryBackend:
    """Local Team Memory adapter with lifecycle, retrieval, and sync semantics."""

    def __init__(self, path: Path | None = None):
        configured_path = str(os.environ.get("REPOSITORY_MEMORY_TEAM_DB") or "").strip()
        self.path = path or (Path(configured_path).expanduser().resolve() if configured_path else data_root() / "team-memory" / "team.sqlite3")

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
                idempotency_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
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
                created_at TEXT NOT NULL
            )"""
        )
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
        try:
            total = int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            by_status = {str(row[0]): int(row[1]) for row in connection.execute("SELECT status, COUNT(*) FROM memories GROUP BY status")}
            by_type = {str(row[0]): int(row[1]) for row in connection.execute("SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type")}
            feedback = int(connection.execute("SELECT COUNT(*) FROM memory_feedback").fetchone()[0])
            active_expired = sum(1 for row in connection.execute("SELECT valid_until FROM memories WHERE status = 'active' AND valid_until IS NOT NULL") if self._expired(row[0]))
            fts = self._fts_available(connection)
        finally:
            connection.close()
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
            "active_expired": active_expired,
            "index": "fts5" if fts else "sqlite-scan",
            "retrieval_strategy": "keyword-only",
            "semantic_available": False,
            "canonical_repo_changed": False,
            "sync": {"export": True, "import": True, "remote_service": False},
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
        supersedes = str(payload.get("supersedes") or "").strip() or None
        valid_from = str(payload.get("valid_from") or now)
        valid_until = str(payload.get("valid_until") or "") or None
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            existing = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if existing:
                return {"schema_version": 1, "ok": True, "duplicate": True, "memory": self._row(existing), "canonical_repo_changed": False}
            connection.execute(
                """INSERT INTO memories
                (id, memory_type, title, content, summary, scope, provenance, confidence, status,
                 supersedes, superseded_by, valid_from, valid_until, author_agent, idempotency_key,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                (memory_id, memory_type, title, content, summary, _json(scope), _json(provenance), confidence, status, supersedes, valid_from, valid_until, author_agent, idempotency, now, now),
            )
            if supersedes:
                connection.execute("UPDATE memories SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ? AND status != 'superseded'", (memory_id, now, supersedes))
            if self._fts_available(connection):
                body = "\n".join((title, summary, content, _json(scope), _json(provenance)))
                connection.execute("INSERT INTO memories_fts (id, body) VALUES (?, ?)", (memory_id, body))
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return {"schema_version": 1, "ok": True, "duplicate": False, "memory": self._row(row), "write_operation": "explicit", "canonical_repo_changed": False}
        return self._write(operation)

    def get(self, memory_id: str, *, include_feedback: bool = True) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise KeyError(f"team memory not found: {memory_id}")
            feedback = connection.execute("SELECT rating, note, agent, created_at FROM memory_feedback WHERE memory_id = ? ORDER BY id", (memory_id,)).fetchall() if include_feedback else []
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
            if self._fts_available(connection) and terms:
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

    def feedback(self, memory_id: str, rating: str, note: str = "", agent: str | None = None) -> dict[str, Any]:
        rating = str(rating).strip().lower()
        if rating not in RATINGS:
            raise ValueError(f"rating must be one of: {', '.join(sorted(RATINGS))}")
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if connection.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
                raise KeyError(f"team memory not found: {memory_id}")
            created_at = _now()
            connection.execute("INSERT INTO memory_feedback (memory_id, rating, note, agent, created_at) VALUES (?, ?, ?, ?, ?)", (memory_id, rating, str(note)[:2000], agent, created_at))
            transition = None
            if rating in {"stale", "wrong"}:
                now = _now()
                connection.execute("UPDATE memories SET confidence = MAX(0, confidence - 0.1), updated_at = ? WHERE id = ?", (now, memory_id))
                if rating == "wrong":
                    connection.execute("UPDATE memories SET status = 'stale', updated_at = ? WHERE id = ? AND status NOT IN ('superseded', 'stale')", (now, memory_id))
                    transition = "stale"
                else:
                    agents = {str(row[0]).strip() for row in connection.execute("SELECT DISTINCT agent FROM memory_feedback WHERE memory_id = ? AND rating = 'stale' AND agent IS NOT NULL AND TRIM(agent) != ''", (memory_id,))}
                    if len(agents) >= 2:
                        connection.execute("UPDATE memories SET status = 'stale', updated_at = ? WHERE id = ? AND status = 'active'", (now, memory_id))
                        transition = "stale"
            status_row = connection.execute("SELECT status FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return {"schema_version": 1, "ok": True, "memory_id": memory_id, "rating": rating, "status": status_row[0] if status_row else None, "lifecycle_transition": transition, "canonical_repo_changed": False}
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
            feedback = [dict(row) for row in connection.execute("SELECT memory_id, rating, note, agent, created_at FROM memory_feedback ORDER BY memory_id, created_at, id").fetchall()]
        finally:
            connection.close()
        return {"schema_version": 1, "kind": "repository-memory-team-bundle", "backend": self.backend_name, "exported_at": _now(), "records": records, "feedback": feedback}

    def import_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(bundle, dict) or bundle.get("kind") != "repository-memory-team-bundle":
            raise ValueError("invalid Team Memory bundle")
        records = bundle.get("records") if isinstance(bundle.get("records"), list) else []
        feedback = bundle.get("feedback") if isinstance(bundle.get("feedback"), list) else []

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            inserted = updated = skipped = conflicts = feedback_added = 0
            for incoming in records:
                if not isinstance(incoming, dict) or not str(incoming.get("id") or "").startswith("team:"):
                    raise ValueError("Team Memory bundle contains an invalid record")
                record = {key: incoming.get(key) for key in ("id", "memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "supersedes", "superseded_by", "valid_from", "valid_until", "author_agent", "idempotency_key", "created_at", "updated_at")}
                if not all(record.get(key) is not None for key in ("id", "memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "created_at", "updated_at")):
                    raise ValueError(f"Team Memory record is incomplete: {record.get('id')}")
                if record["memory_type"] not in MEMORY_TYPES or record["status"] not in STATUSES:
                    raise ValueError(f"Team Memory record has an unsupported type/status: {record.get('id')}")
                if SECRET_CONTENT.search(f"{record['title']}\n{record['content']}"):
                    raise ValueError(f"Team Memory record contains a secret-like value: {record.get('id')}")
                existing = connection.execute("SELECT * FROM memories WHERE id = ?", (record["id"],)).fetchone()
                if existing is None:
                    connection.execute(
                        """INSERT INTO memories (id, memory_type, title, content, summary, scope, provenance, confidence, status, supersedes, superseded_by, valid_from, valid_until, author_agent, idempotency_key, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        tuple(record[key] for key in ("id", "memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "supersedes", "superseded_by", "valid_from", "valid_until", "author_agent", "idempotency_key", "created_at", "updated_at")),
                    )
                    if self._fts_available(connection):
                        body = "\n".join((str(record["title"]), str(record["summary"]), str(record["content"]), str(record["scope"]), str(record["provenance"])))
                        connection.execute("INSERT OR REPLACE INTO memories_fts (id, body) VALUES (?, ?)", (record["id"], body))
                    inserted += 1
                elif str(record["updated_at"]) > str(existing["updated_at"]):
                    connection.execute(
                        """UPDATE memories SET memory_type=?, title=?, content=?, summary=?, scope=?, provenance=?, confidence=?, status=?, supersedes=?, superseded_by=?, valid_from=?, valid_until=?, author_agent=?, idempotency_key=?, created_at=?, updated_at=? WHERE id=?""",
                        tuple(record[key] for key in ("memory_type", "title", "content", "summary", "scope", "provenance", "confidence", "status", "supersedes", "superseded_by", "valid_from", "valid_until", "author_agent", "idempotency_key", "created_at", "updated_at")) + (record["id"],),
                    )
                    if self._fts_available(connection):
                        connection.execute("DELETE FROM memories_fts WHERE id = ?", (record["id"],))
                        body = "\n".join((str(record["title"]), str(record["summary"]), str(record["content"]), str(record["scope"]), str(record["provenance"])))
                        connection.execute("INSERT INTO memories_fts (id, body) VALUES (?, ?)", (record["id"], body))
                    updated += 1
                elif any(existing[key] != record[key] for key in ("content", "status", "updated_at")):
                    conflicts += 1
                else:
                    skipped += 1
            for item in feedback:
                if not isinstance(item, dict) or not item.get("memory_id"):
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM memory_feedback WHERE memory_id = ? AND rating = ? AND note = ? AND COALESCE(agent, '') = COALESCE(?, '') AND created_at = ?",
                    (item.get("memory_id"), item.get("rating"), item.get("note") or "", item.get("agent"), item.get("created_at")),
                ).fetchone()
                if exists:
                    continue
                connection.execute("INSERT INTO memory_feedback (memory_id, rating, note, agent, created_at) VALUES (?, ?, ?, ?, ?)", (item.get("memory_id"), item.get("rating"), item.get("note") or "", item.get("agent"), item.get("created_at") or _now()))
                feedback_added += 1
            return {"inserted": inserted, "updated": updated, "skipped": skipped, "conflicts": conflicts, "feedback_added": feedback_added}

        result = self._write(operation)
        return {"schema_version": 1, "ok": True, "imported": result, "canonical_repo_changed": False}


TeamMemoryStore = SQLiteTeamMemoryBackend


class TeamMemoryBackend(Protocol):
    """Stable seam used by the runtime; concrete storage stays behind it."""

    def health(self) -> dict[str, Any]: ...
    def publish(self, payload: dict[str, Any], *, default_status: str = "candidate") -> dict[str, Any]: ...
    def get(self, memory_id: str, *, include_feedback: bool = True) -> dict[str, Any]: ...
    def search(self, query: str, *, limit: int = 10, repo: str | None = None, issue: str | None = None, branch: str | None = None, agent: str | None = None, include_candidates: bool = True) -> dict[str, Any]: ...
    def feedback(self, memory_id: str, rating: str, note: str = "", agent: str | None = None) -> dict[str, Any]: ...
    def export_bundle(self) -> dict[str, Any]: ...
    def import_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]: ...


def team_memory_backend() -> TeamMemoryBackend:
    backend = str(os.environ.get("REPOSITORY_MEMORY_TEAM_BACKEND") or "sqlite").strip().lower()
    if backend not in {"sqlite", ""}:
        raise RuntimeError(f"unsupported Team Memory backend '{backend}'; supported backend: sqlite")
    return SQLiteTeamMemoryBackend()


def team_memory_store() -> TeamMemoryStore:
    return team_memory_backend()  # type: ignore[return-value]
