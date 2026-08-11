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
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


class TeamMemoryStore:
    """Small deep module for shared knowledge lifecycle and retrieval."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_root() / "team-memory" / "team.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
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
        connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return connection

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
            fts = self._fts_available(connection)
        finally:
            connection.close()
        return {
            "backend": "team-memory-sqlite",
            "configured": True,
            "reachable": True,
            "status": "ready",
            "path": str(self.path),
            "record_count": total,
            "by_status": by_status,
            "by_type": by_type,
            "feedback_count": feedback,
            "index": "fts5" if fts else "sqlite-scan",
            "retrieval_strategy": "keyword-only",
            "semantic_available": False,
            "canonical_repo_changed": False,
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
        if row["status"] == "candidate":
            evidence_status = "candidate"
        elif row["status"] in {"stale", "superseded"}:
            evidence_status = row["status"]
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
        connection = self._connect()
        try:
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
            connection.commit()
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        finally:
            connection.close()
        return {"schema_version": 1, "ok": True, "duplicate": False, "memory": self._row(row), "write_operation": "explicit", "canonical_repo_changed": False}

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
        for row in rows:
            if row["status"] == "superseded" or row["status"] == "stale":
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
            "diagnostics": {"result_count": len(active), "candidate_count": len(candidates), "query_terms": terms, "filters": {"repo": repo, "issue": issue, "branch": branch, "agent": agent}},
        }

    def feedback(self, memory_id: str, rating: str, note: str = "", agent: str | None = None) -> dict[str, Any]:
        rating = str(rating).strip().lower()
        if rating not in RATINGS:
            raise ValueError(f"rating must be one of: {', '.join(sorted(RATINGS))}")
        connection = self._connect()
        try:
            if connection.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
                raise KeyError(f"team memory not found: {memory_id}")
            connection.execute("INSERT INTO memory_feedback (memory_id, rating, note, agent, created_at) VALUES (?, ?, ?, ?, ?)", (memory_id, rating, str(note)[:2000], agent, _now()))
            if rating in {"stale", "wrong"}:
                connection.execute("UPDATE memories SET confidence = MAX(0, confidence - 0.1), updated_at = ? WHERE id = ?", (_now(), memory_id))
            connection.commit()
        finally:
            connection.close()
        return {"schema_version": 1, "ok": True, "memory_id": memory_id, "rating": rating, "canonical_repo_changed": False}


def team_memory_store() -> TeamMemoryStore:
    return TeamMemoryStore()
