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
    assert list((repository / "knowledge/team-memory/l1/active").glob("*.md"))

    # A fresh local node can hydrate the shared record without reading raw
    # conversations or depending on a network service.
    fresh_db = tmp_path / "fresh.sqlite3"
    monkeypatch.setenv("REPOSITORY_MEMORY_TEAM_DB", str(fresh_db))
    fresh = SQLiteTeamMemoryBackend(fresh_db, node_id="fresh-node")
    result = import_team_memory(str(repository), include_candidates=False)
    assert result["ok"] is True
    assert result["imported"] == 1
    assert fresh.search("reusable failure", include_candidates=False)["active"]
    health = team_repository_health(str(repository))
    assert health["configured"] is True
    assert health["reachable"] is True
