#!/usr/bin/env python3
"""Git-backed Team Memory bridge.

The local SQLite store is the fast, private runtime.  This module is the
small, explicit bridge to a user-owned canonical team repository.  It writes
only reviewable Markdown candidates/records, never raw conversations, and it
never commits or pushes on the caller's behalf.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from discovery import config_path, data_root, read_config
from team_memory import team_memory_store


TEAM_REPO_ENV = "REPOSITORY_MEMORY_TEAM_REPOSITORY"
TEAM_AUTO_SYNC_ENV = "REPOSITORY_MEMORY_TEAM_AUTO_SYNC"
TEAM_DIR = Path("knowledge/team-memory")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _team_config() -> dict[str, Any]:
    value = read_config().get("team_memory")
    return value if isinstance(value, dict) else {}


def configured_team_repository(explicit: str | None = None) -> Path | None:
    value = explicit or os.environ.get(TEAM_REPO_ENV) or _team_config().get("repository_root")
    if not value:
        return None
    root = Path(str(value)).expanduser().resolve()
    if not (root / TEAM_DIR / "README.md").is_file():
        raise RuntimeError(f"team repository is not a supported Git Team Memory source: {root}")
    return root


def _write_config(value: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def configure_team_repository(repository: str, *, auto_sync: bool = True, agent_id: str | None = None) -> dict[str, Any]:
    root = configured_team_repository(repository)
    if root is None:
        raise RuntimeError("team repository path is required")
    config = read_config()
    current = _team_config()
    updated = {**current, "repository_root": str(root), "auto_sync": bool(auto_sync)}
    if agent_id:
        updated["agent_id"] = str(agent_id)
    config["team_memory"] = updated
    path = _write_config(config)
    return {
        "ok": True,
        "repository_root": str(root),
        "auto_sync": bool(auto_sync),
        "config": str(path),
        "canonical_repo_changed": False,
    }


def auto_sync_enabled() -> bool:
    configured = _team_config().get("auto_sync")
    if configured is not None:
        return bool(configured)
    return _truthy(os.environ.get(TEAM_AUTO_SYNC_ENV, "0"))


def _parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _safe(value: str, fallback: str = "unknown") -> str:
    result = _SAFE.sub("-", str(value or "").strip()).strip("-.")
    return result[:80] or fallback


def _central_id(memory_id: str, layer: str = "L1") -> str:
    # Records hydrated from the canonical repository keep the central ID in
    # their local source ID.  Reusing it is what makes export/import
    # idempotent across processes and machines; hashing the wrapper would
    # create a second active file for the same memory.
    value = str(memory_id or "")
    if value.startswith("team:central:"):
        value = value.split("team:central:", 1)[1]
    if re.fullmatch(r"team_l[123]_[a-f0-9]{24}", value):
        return value
    digest = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:24]
    return f"team_{layer.lower()}_{digest}"


def _record_central_id(record: dict[str, Any], layer: str | None = None) -> str:
    provenance = _provenance(record)
    central_id = str(provenance.get("central_id") or "")
    if re.fullmatch(r"team_l[123]_[a-f0-9]{24}", central_id):
        return central_id
    return _central_id(str(record.get("id") or ""), layer or _layer(record))


def _yaml(value: Any) -> str:
    if value in (None, ""):
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _provenance(record: dict[str, Any]) -> dict[str, Any]:
    return _parse_json(record.get("provenance"), {}) if record.get("provenance") else {}


def _scope(record: dict[str, Any]) -> dict[str, Any]:
    return _parse_json(record.get("scope"), {}) if record.get("scope") else {}


def _layer(record: dict[str, Any]) -> str:
    provenance = _provenance(record)
    value = str(record.get("layer") or provenance.get("layer") or "L1").upper()
    return value if value in {"L1", "L2", "L3"} else "L1"


def _status_path(root: Path, record: dict[str, Any]) -> Path:
    layer = _layer(record)
    status = str(record.get("status") or "candidate").lower()
    provenance = _provenance(record)
    agent = _safe(str(record.get("author_agent") or provenance.get("agent_id") or provenance.get("agent") or "unknown"))
    central_id = _record_central_id(record, layer)
    if layer == "L1":
        if status == "candidate":
            return root / TEAM_DIR / "inbox" / agent / f"{central_id}.md"
        bucket = status if status in {"active", "stale", "superseded"} else "candidate"
        return root / TEAM_DIR / "l1" / bucket / f"{central_id}.md"
    bucket = "accepted" if status in {"accepted", "active"} else "candidate"
    return root / TEAM_DIR / layer.lower() / bucket / f"{central_id}.md"


def _evidence_lines(record: dict[str, Any]) -> list[str]:
    provenance = _provenance(record)
    evidence = provenance.get("evidence") or provenance.get("citations") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    lines: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        parts = []
        for key in ("repository", "commit", "path", "line_start", "line_end", "locator"):
            if item.get(key) not in (None, ""):
                parts.append(f"{key}={item[key]}")
        if parts:
            lines.append("- " + "; ".join(parts))
    return lines


def _markdown(record: dict[str, Any], *, central_id: str, layer: str, status: str, agent: str) -> str:
    provenance = _provenance(record)
    scope = _scope(record)
    source_id = str(provenance.get("source_memory_id") or record.get("id") or "")
    lines = [
        "---",
        f"id: {central_id}",
        "schema_version: 1",
        f"layer: {layer}",
        f"kind: {record.get('memory_type') or 'discovery'}",
        f"status: {status}",
        "content_type: team_memory",
        f"scope: {_yaml(scope)}",
        "provenance:",
        f"  agent_id: {_yaml(agent)}",
        f"  observed_at: {_yaml(provenance.get('observed_at') or record.get('created_at'))}",
        f"  run_id: {_yaml(provenance.get('run_id'))}",
        f"  session_id: {_yaml(provenance.get('session_id'))}",
        f"  source_memory_id: {_yaml(source_id)}",
        "  source_type: local_team_memory",
        f"confidence: {_yaml(record.get('confidence', 0.0))}",
        f"valid_until: {_yaml(record.get('valid_until'))}",
    ]
    reviewer = record.get("reviewed_by")
    if reviewer:
        lines.append(f"reviewed_by: {_yaml(reviewer)}")
    if record.get("activated_at"):
        lines.append(f"accepted_at: {_yaml(record.get('activated_at'))}")
    lines.extend(["evidence:"])
    evidence = _evidence_lines(record)
    lines.extend(evidence or ["  - citation_status: pending_git_link"])
    lines.extend(["---", "", f"# {record.get('title') or 'Team memory'}", ""])
    summary = str(record.get("summary") or "").strip()
    content = str(record.get("content") or "").strip()
    if summary and summary != content:
        lines.extend(["## Summary", "", summary, ""])
    lines.extend(["## Content", "", content, ""])
    if evidence:
        lines.extend(["## Evidence", "", *evidence, ""])
    return "\n".join(lines)


def _existing_by_id(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    base = root / TEAM_DIR
    if not base.is_dir():
        return result
    for path in base.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"^id:\s*([^\n]+)", text, re.MULTILINE)
        if match:
            result[match.group(1).strip().strip('"')] = path
    return result


def _catalog(root: Path) -> str:
    rows = [
        "# Team Memory Catalog", "", "Generated from `knowledge/team-memory`; candidates are not default facts.", "",
        "| ID | Layer | Status | Kind | Origin |", "|---|---|---|---|---|",
    ]
    entries: list[tuple[str, str, str, str, str]] = []
    for path in sorted((root / TEAM_DIR).rglob("*.md")):
        if path.name in {"README.md", "CATALOG.md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fields = {}
        for key in ("id", "layer", "status", "kind"):
            match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
            fields[key] = match.group(1).strip().strip('"') if match else ""
        origin = re.search(r"^\s*agent_id:\s*(.+)$", text, re.MULTILINE)
        entries.append((fields["id"], fields["layer"], fields["status"], fields["kind"], origin.group(1).strip().strip('"') if origin else ""))
    for row in entries:
        rows.append("| " + " | ".join(f"`{item}`" if index == 0 else item for index, item in enumerate(row)) + " |")
    return "\n".join(rows) + "\n"


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``mkstemp`` returns an *open* descriptor.  Taking only the name leaked
    # that descriptor, and on Windows an open handle makes ``os.replace`` fail
    # with WinError 32 -- every export there died on its first record.  Write
    # through the descriptor and close it before the rename.
    handle_fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def export_team_memory(repository: str | None = None, *, agent_id: str | None = None) -> dict[str, Any]:
    root = configured_team_repository(repository)
    if root is None:
        return {"ok": False, "status": "not_configured", "reason": "team repository is not configured", "canonical_repo_changed": False}
    existing = _existing_by_id(root)
    created = skipped = preserved = conflicts = moved = 0
    files: list[str] = []
    bundle = team_memory_store().export_bundle()
    # A pull hydrates the same central memory into the local store.  Keep the
    # original local record when both forms are present; otherwise two local
    # rows would alternately rewrite the same canonical file on every sync.
    records_by_central_id: dict[str, dict[str, Any]] = {}
    for candidate in bundle.get("records", []):
        if not isinstance(candidate, dict):
            continue
        candidate_id = _record_central_id(candidate, _layer(candidate))
        current = records_by_central_id.get(candidate_id)
        if current is None or "central_id" in _provenance(current):
            records_by_central_id[candidate_id] = candidate
    for record in records_by_central_id.values():
        if not isinstance(record, dict):
            continue
        provenance = _provenance(record)
        origin = str(record.get("author_agent") or provenance.get("agent_id") or provenance.get("agent") or "unknown")
        if agent_id and origin != agent_id:
            continue
        layer = _layer(record)
        status = str(record.get("status") or "candidate")
        central_id = _record_central_id(record, layer)
        content = _markdown(record, central_id=central_id, layer=layer, status=status, agent=origin)
        target = _status_path(root, record)
        prior = existing.get(central_id)
        if prior and prior != target:
            prior_content = prior.read_text(encoding="utf-8")
            if prior_content == content:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() and "inbox" in prior.parts:
                    prior.replace(target)
                    moved += 1
                    files.append(str(target.relative_to(root)))
                else:
                    skipped += 1
                continue
            # A lifecycle transition is a move, not a second memory.  Only
            # move files that are already inside this repository's inbox; an
            # unrelated existing file is preserved and reported as a conflict.
            if "inbox" in prior.parts and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                prior.replace(target)
                _write_if_changed(target, content)
                moved += 1
                files.append(str(target.relative_to(root)))
                continue
            conflict = root / TEAM_DIR / "conflicts" / f"{central_id}-{hashlib.sha256(content.encode()).hexdigest()[:10]}.md"
            if _write_if_changed(conflict, content):
                conflicts += 1
                files.append(str(conflict.relative_to(root)))
            continue
        if prior == target and prior.is_file():
            prior_content = prior.read_text(encoding="utf-8")
            if prior_content == content:
                skipped += 1
                continue
            # Canonical records are append/review controlled.  A local
            # hydrated wrapper may have less provenance than the canonical
            # Markdown (for example after a pull/read-back), so an automatic
            # sync must never replace an existing file in-place.  This is
            # especially important for active/accepted records, whose review
            # evidence is not reproducible from the local SQLite projection.
            # Keep the canonical file and make the discrepancy visible to the
            # caller instead of silently downgrading it.
            preserved += 1
            skipped += 1
            continue
        if _write_if_changed(target, content):
            created += 1
            files.append(str(target.relative_to(root)))
        else:
            skipped += 1
    catalog = root / TEAM_DIR / "CATALOG.md"
    catalog_changed = _write_if_changed(catalog, _catalog(root))
    changed = bool(created or moved or conflicts or catalog_changed)
    return {
        "ok": True,
        "status": "synced",
        "repository_root": str(root),
        "created": created,
        "skipped": skipped,
        "preserved": preserved,
        "moved": moved,
        "conflicts": conflicts,
        "catalog_changed": catalog_changed,
        "files": files,
        "push_required": changed,
        "canonical_repo_changed": changed,
        "note": "Files were written locally; no git commit or push was performed.",
    }


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text
    fields: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip()
        # Keep the small provenance fields needed for hydration.  Other
        # nested YAML remains human-readable in the canonical file and is not
        # interpreted as executable configuration.
        if normalized in {"id", "layer", "status", "kind", "confidence", "valid_until", "accepted_at", "reviewed_by", "agent_id", "source_memory_id", "observed_at", "run_id", "session_id", "scope"}:
            cleaned = value.strip().strip('"')
            fields[normalized] = "" if cleaned.lower() == "null" else cleaned
    return fields, parts[2].strip()


def import_team_memory(repository: str | None = None, *, include_candidates: bool = True) -> dict[str, Any]:
    root = configured_team_repository(repository)
    if root is None:
        return {"ok": False, "status": "not_configured", "reason": "team repository is not configured", "canonical_repo_changed": False}
    paths: list[Path] = []
    base = root / TEAM_DIR
    for relative in ("l1/active", "l1/stale", "l2/accepted", "l3/accepted"):
        paths.extend((base / relative).glob("*.md"))
    if include_candidates:
        paths.extend((base / "inbox").glob("*/*.md"))
        paths.extend((base / "l1/candidate").glob("*.md"))
        paths.extend((base / "l2/candidate").glob("*.md"))
        paths.extend((base / "l3/candidate").glob("*.md"))
    imported = skipped = failed = 0
    failures: list[dict[str, str]] = []
    store = team_memory_store()
    for path in sorted(paths):
        # Best-effort per record: one unreadable or malformed canonical file
        # must not abort the whole pull.  The whole per-file body sits inside
        # the guard -- a bad ``confidence:`` or an unreadable file used to
        # escape the old narrower try and kill the entire hydration.  And the
        # failure names its file: two hosts reproduced ``failed: 1`` and
        # neither could say which record it was.
        try:
            fields, body = _frontmatter(path.read_text(encoding="utf-8"))
            central_id = fields.get("id")
            if not central_id:
                continue
            status = fields.get("status", "candidate")
            if status == "accepted":
                local_status = "active"
            elif status in {"active", "candidate", "stale", "superseded"}:
                local_status = status
            else:
                continue
            title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else central_id
            summary_match = re.search(r"^## Summary\s*\n\s*(.*?)(?=\n## Content\s*\n|\Z)", body, re.MULTILINE | re.DOTALL)
            content_match = re.search(r"^## Content\s*\n\s*(.*?)(?=\n## Evidence\s*\n|\Z)", body, re.MULTILINE | re.DOTALL)
            summary = summary_match.group(1).strip() if summary_match else ""
            content = content_match.group(1).strip() if content_match else body[:12000]
            evidence: list[dict[str, str]] = []
            evidence_match = re.search(r"^evidence:\s*\n(?P<items>(?:\s*- .*\n?)*)", path.read_text(encoding="utf-8"), re.MULTILINE)
            for item in (evidence_match.group("items").splitlines() if evidence_match else []):
                raw = item.strip()
                if not raw.startswith("-"):
                    continue
                parsed: dict[str, str] = {}
                for part in raw[1:].strip().split("; "):
                    if "=" in part:
                        key, value = part.split("=", 1)
                        parsed[key.strip()] = value.strip()
                if parsed:
                    evidence.append(parsed)
            source_memory_id = fields.get("source_memory_id")
            scope = _parse_json(fields.get("scope"), {})
            provenance = {
                "source": "team-knowledge-data",
                "canonical_path": str(path.relative_to(root)),
                "layer": fields.get("layer", "L1"),
                "agent_id": fields.get("agent_id"),
                "central_id": central_id,
                "source_memory_id": source_memory_id,
                "observed_at": fields.get("observed_at"),
                "run_id": fields.get("run_id"),
                "session_id": fields.get("session_id"),
                "evidence": evidence,
            }
            payload = {
                # The canonical ID is the identity shared across local stores.
                # Keep the original local ID only as provenance; otherwise a
                # hydrated record would hash to a second central filename when it
                # is exported again.
                "id": f"team:central:{central_id}",
                "type": fields.get("kind", "discovery"),
                "title": title,
                "content": content[:12000],
                "summary": summary or content[:400],
                # A newly hydrated record can only enter the local store as
                # candidate or active.  Canonical ``stale``/``superseded`` states
                # land as candidate for review, matching the default_status guard
                # below; the canonical state stays visible in provenance.
                "status": local_status if local_status in {"candidate", "active"} else "candidate",
                "confidence": float(fields.get("confidence") or 0.5),
                "scope": scope,
                "provenance": {**provenance, "canonical_status": local_status},
                "reviewed_by": fields.get("reviewed_by"),
                "activated_at": fields.get("accepted_at"),
                "idempotency_key": f"central:{central_id}",
            }
            result = store.publish(payload, default_status=local_status if local_status in {"candidate", "active"} else "candidate")
        except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
            failed += 1
            if len(failures) < 20:
                failures.append({"path": str(path.relative_to(root)), "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        if result.get("duplicate"):
            skipped += 1
        else:
            imported += 1
    return {"ok": True, "status": "hydrated", "repository_root": str(root), "imported": imported, "skipped": skipped, "failed": failed, "failures": failures, "canonical_repo_changed": False}


def sync_team_memory(repository: str | None = None, *, agent_id: str | None = None, pull: bool = True) -> dict[str, Any]:
    exported = export_team_memory(repository, agent_id=agent_id)
    if not exported.get("ok"):
        return exported
    imported = import_team_memory(repository, include_candidates=True) if pull else {"ok": True, "status": "skipped", "imported": 0, "skipped": 0}
    return {**exported, "pull": imported, "status": "synced", "canonical_repo_changed": bool(exported.get("canonical_repo_changed"))}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result


def publish_team_memory(repository: str | None = None, *, agent_id: str | None = None, pull: bool = True, push: bool = True) -> dict[str, Any]:
    """Pull, sync, and publish the team memory Git plane in one explicit step.

    The capture hook deliberately never commits or pushes, so every node used
    to close that gap with a hand-written shell script passed around in chat —
    the second host got its copy by prompt. This is that script as a first-
    class command: rebase-pull the team repository, run the same team-sync,
    and commit/push only what team-sync wrote under ``knowledge/``. Review is
    deliberately not here: activation stays an explicit supervised step.
    """

    root = configured_team_repository(repository)
    if root is None:
        return {"ok": False, "status": "not_configured", "reason": "team repository is not configured", "canonical_repo_changed": False}
    if not (root / ".git").exists():
        return {"ok": False, "status": "not_a_git_repository", "repository_root": str(root), "canonical_repo_changed": False}
    # Commit needs an author. Preflight it instead of letting git fail in
    # whatever language the host speaks -- the first fresh-host run died on
    # a localized "Author identity unknown" that the JSON could not explain.
    if not _git(root, "config", "user.email", check=False).stdout.strip():
        return {
            "ok": False,
            "status": "missing_git_identity",
            "repository_root": str(root),
            "reason": f"set an identity first: git -C {root} config user.name <name> && git -C {root} config user.email <email>",
            "canonical_repo_changed": False,
        }
    result: dict[str, Any] = {"ok": True, "operation": "team-publish", "repository_root": str(root), "pulled": False, "committed": False, "pushed": False, "commit": None, "canonical_repo_changed": False}
    try:
        if pull and _git(root, "remote", check=False).stdout.strip():
            _git(root, "pull", "--rebase", "--autostash", "--quiet")
            result["pulled"] = True
        sync = sync_team_memory(repository, agent_id=agent_id, pull=True)
        result["sync"] = {key: sync.get(key) for key in ("ok", "created", "moved", "conflicts", "preserved")}
        result["pull_hydrate"] = {key: (sync.get("pull") or {}).get(key) for key in ("imported", "skipped", "failed", "failures")}
        if not sync.get("ok"):
            result["ok"] = False
            return result
        # Stage only the knowledge tree team-sync writes into. ``add -A`` at
        # the repository root would also sweep whatever else happens to sit in
        # the clone -- publishing must never turn into a junk drawer commit.
        _git(root, "add", "--", "knowledge")
        if _git(root, "status", "--porcelain", "--", "knowledge").stdout.strip():
            stamp = dt.date.today().isoformat()
            author = agent_id or team_memory_store().node_id
            _git(root, "commit", "--quiet", "-m", f"chore(team-memory): publish from {author} {stamp}")
            result["committed"] = True
            result["commit"] = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
            if push and _git(root, "remote", check=False).stdout.strip():
                _git(root, "push", "--quiet")
                result["pushed"] = True
    except RuntimeError as exc:
        result["ok"] = False
        result["error"] = str(exc)
    return result


def distinct_memory_counts() -> dict[str, Any]:
    """Count memories, not rows, grouped by canonical identity.

    One memory can sit in the local store as two rows -- the local original
    and a central wrapper hydrated back from the canonical repository -- so
    ``by_status`` row counts overstate the store.  Measured consequence: a
    fresh host hydrated 75 active canonical files and was told to expect
    "140+" because this machine's row count said 143.  The distinct view is
    what the Git plane holds; a group counts as its most advanced lifecycle
    state so a half-propagated activation is not reported as candidate.
    """

    groups: dict[str, list[str]] = {}
    for record in team_memory_store().export_bundle().get("records", []):
        if not isinstance(record, dict):
            continue
        groups.setdefault(_record_central_id(record, _layer(record)), []).append(str(record.get("status") or "candidate"))
    order = ("active", "superseded", "stale", "candidate")
    by_status: dict[str, int] = {}
    for statuses in groups.values():
        effective = next((state for state in order if state in statuses), statuses[0])
        by_status[effective] = by_status.get(effective, 0) + 1
    return {"total": len(groups), "by_status": by_status}


def team_repository_health(repository: str | None = None) -> dict[str, Any]:
    root = configured_team_repository(repository)
    if root is None:
        return {"configured": False, "reachable": False, "status": "not_configured", "canonical_repo_changed": False}
    files = list((root / TEAM_DIR).rglob("*.md"))
    return {
        "configured": True,
        "reachable": True,
        "status": "ready",
        "repository_root": str(root),
        "file_count": len(files),
        "auto_sync": auto_sync_enabled(),
        "canonical_repo_changed": False,
    }
