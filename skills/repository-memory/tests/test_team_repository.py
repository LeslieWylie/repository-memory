from __future__ import annotations

from team_memory import SQLiteTeamMemoryBackend
from team_repository import configure_team_repository, import_team_memory, sync_team_memory, team_repository_health


def test_team_repository_export_is_idempotent_and_hydrates(tmp_path, monkeypatch):
    repository = tmp_path / "team-data"
    (repository / "knowledge/team-memory").mkdir(parents=True)
    (repository / "knowledge/team-memory/README.md").write_text("# Shared Team Memory\n", encoding="utf-8")
    monkeypatch.setenv("REPOSITORY_MEMORY_TEAM_DB", str(tmp_path / "team.sqlite3"))
    monkeypatch.setenv("REPOSITORY_MEMORY_CONFIG", str(tmp_path / "config.json"))

    backend = SQLiteTeamMemoryBackend(tmp_path / "team.sqlite3", node_id="test-node")
    published = backend.publish(
        {
            "type": "failure",
            "title": "Test failure",
            "content": "A reusable failure with a source citation.",
            "status": "candidate",
            "author_agent": "yaole",
            "provenance": {
                "agent_id": "yaole",
                "session_id": "session-1",
                "evidence": [{"repository": "demo", "commit": "abc123", "path": "docs/fix.md", "line_start": 4, "line_end": 9}],
            },
            "idempotency_key": "test-capture-1",
        }
    )
    assert published["ok"] is True
    configure_team_repository(str(repository), auto_sync=True, agent_id="yaole")

    first = sync_team_memory()
    assert first["ok"] is True
    assert first["created"] >= 1
    assert list((repository / "knowledge/team-memory/inbox/yaole").glob("*.md"))
    assert "citation_status" not in next((repository / "knowledge/team-memory/inbox/yaole").glob("*.md")).read_text(encoding="utf-8")

    second = sync_team_memory()
    assert second["ok"] is True
    assert second["created"] == 0
    assert second["conflicts"] == 0

    active = backend.activate(published["memory"]["id"], reviewer="reviewer")
    assert active["status"] == "active"
    sync_team_memory()
    active_path = next((repository / "knowledge/team-memory/l1/active").glob("*.md"))
    assert active_path.is_file()
    canonical_marker = "reviewed canonical evidence must survive local hydration"
    active_path.write_text(active_path.read_text(encoding="utf-8") + f"\n{canonical_marker}\n", encoding="utf-8")
    protected = sync_team_memory()
    assert protected["preserved"] >= 1
    assert canonical_marker in active_path.read_text(encoding="utf-8")

    # A fresh local node can hydrate the shared record without reading raw
    # conversations or depending on a network service.
    fresh_db = tmp_path / "fresh.sqlite3"
    monkeypatch.setenv("REPOSITORY_MEMORY_TEAM_DB", str(fresh_db))
    fresh = SQLiteTeamMemoryBackend(fresh_db, node_id="fresh-node")
    result = import_team_memory(str(repository), include_candidates=False)
    assert result["ok"] is True
    assert result["imported"] == 1
    assert fresh.search("reusable failure", include_candidates=False)["active"]
    # A hydrated central record must export back to the same filename.  This
    # guards against creating a duplicate active memory after a pull.
    round_trip = sync_team_memory(str(repository), pull=False)
    assert round_trip["ok"] is True
    assert round_trip["created"] == 0
    assert len(list((repository / "knowledge/team-memory/l1/active").glob("*.md"))) == 1
    health = team_repository_health(str(repository))
    assert health["configured"] is True
    assert health["reachable"] is True


def test_publish_matches_existing_idempotency_key_under_a_changed_id_scheme(tmp_path):
    """A re-publish must return a duplicate receipt, not an IntegrityError.

    ``idempotency_key`` is UNIQUE.  Records written under an earlier id scheme
    still own their key, so matching on the id alone raised
    ``sqlite3.IntegrityError`` out of ``publish`` and aborted the caller's whole
    turn capture.
    """

    backend = SQLiteTeamMemoryBackend(tmp_path / "team.sqlite3", node_id="test-node")
    legacy = backend.publish(
        {
            # The legacy id scheme hashed type/title/content instead of
            # carrying the canonical central id.
            "id": "team:discovery:legacyhash0000",
            "type": "discovery",
            "title": "Legacy hydrated record",
            "content": "Imported before the canonical id scheme changed.",
            "status": "candidate",
            "idempotency_key": "central:team_l1_collision",
        }
    )
    assert legacy["ok"] is True
    assert legacy["duplicate"] is False

    rehydrated = backend.publish(
        {
            "id": "team:central:team_l1_collision",
            "type": "discovery",
            "title": "Legacy hydrated record",
            "content": "Imported before the canonical id scheme changed.",
            "status": "candidate",
            "idempotency_key": "central:team_l1_collision",
        }
    )
    assert rehydrated["ok"] is True
    assert rehydrated["duplicate"] is True
    assert rehydrated["memory"]["id"] == "team:discovery:legacyhash0000"


def test_import_counts_rejected_records_instead_of_aborting(tmp_path, monkeypatch):
    """One unusable canonical file must not stop the rest of the hydration,
    and the result must name it: two hosts reproduced ``failed: 1`` and
    neither could say which record it was."""

    repository = tmp_path / "team-data"
    active = repository / "knowledge/team-memory/l1/active"
    active.mkdir(parents=True)
    (repository / "knowledge/team-memory/README.md").write_text("# Shared Team Memory\n", encoding="utf-8")
    monkeypatch.setenv("REPOSITORY_MEMORY_TEAM_DB", str(tmp_path / "team.sqlite3"))
    monkeypatch.setenv("REPOSITORY_MEMORY_CONFIG", str(tmp_path / "config.json"))

    def write(name: str, central_id: str, kind: str, confidence: str = "0.5") -> None:
        (active / name).write_text(
            "---\n"
            f"id: {central_id}\n"
            f"kind: {kind}\n"
            "status: active\n"
            f"confidence: {confidence}\n"
            "---\n\n"
            f"# {central_id}\n\n"
            "## Summary\n\nSummary line.\n\n"
            "## Content\n\nReusable content body.\n",
            encoding="utf-8",
        )

    # ``scenario`` is the supervisor's own L2 kind; the store must accept what
    # the same pipeline exports.
    write("scenario.md", "team_l2_scenario", "scenario")
    write("good.md", "team_l1_supported", "discovery")
    write("bad-kind.md", "team_l1_bad_kind", "not-a-kind")
    # A malformed confidence used to escape the narrower try and abort the
    # entire hydration; now it is one counted, named failure.
    write("bad-confidence.md", "team_l1_bad_confidence", "discovery", confidence="not-a-number")
    configure_team_repository(str(repository), auto_sync=True, agent_id="yaole")

    result = import_team_memory(include_candidates=False)
    assert result["ok"] is True
    assert result["imported"] == 2
    assert result["failed"] == 2
    named = {failure["path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for failure in result["failures"]}
    assert named == {"bad-kind.md", "bad-confidence.md"}
    assert all(failure["error"] for failure in result["failures"])


def test_import_hydrates_stale_canonical_records_as_candidates(tmp_path, monkeypatch):
    """Canonical ``stale`` records are reviewable candidates, not hard errors."""

    repository = tmp_path / "team-data"
    stale = repository / "knowledge/team-memory/l1/stale"
    stale.mkdir(parents=True)
    (repository / "knowledge/team-memory/README.md").write_text("# Shared Team Memory\n", encoding="utf-8")
    monkeypatch.setenv("REPOSITORY_MEMORY_TEAM_DB", str(tmp_path / "team.sqlite3"))
    monkeypatch.setenv("REPOSITORY_MEMORY_CONFIG", str(tmp_path / "config.json"))
    (stale / "stale.md").write_text(
        "---\n"
        "id: team_l1_stale\n"
        "kind: discovery\n"
        "status: stale\n"
        "confidence: 0.5\n"
        "---\n\n"
        "# team_l1_stale\n\n"
        "## Summary\n\nSummary line.\n\n"
        "## Content\n\nReusable content body.\n",
        encoding="utf-8",
    )
    configure_team_repository(str(repository), auto_sync=True, agent_id="yaole")

    result = import_team_memory(include_candidates=False)
    assert result["ok"] is True
    assert result["imported"] == 1
    assert result["failed"] == 0

    backend = SQLiteTeamMemoryBackend(tmp_path / "team.sqlite3", node_id="test-node")
    record = backend.get("team:central:team_l1_stale")["result"]
    assert record["status"] == "candidate"
    assert record["provenance"]["canonical_status"] == "stale"
