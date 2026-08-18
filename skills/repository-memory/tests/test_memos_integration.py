from __future__ import annotations

import json
from pathlib import Path

from memos_integration import _plugin_state, _update_openclaw, configure, doctor, disable


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "MemOS"
    plugin = root / "apps" / "memos-local-openclaw"
    plugin.mkdir(parents=True)
    (plugin / "package.json").write_text(
        json.dumps({"name": "@memtensor/memos-local-openclaw-plugin", "version": "test", "engines": {"node": ">=18"}}),
        encoding="utf-8",
    )
    return root


def _openclaw(tmp_path: Path) -> Path:
    path = tmp_path / "openclaw.json"
    path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"memorySearch": {"enabled": True}}, "list": [{"id": "yaole", "tools": {}}]},
                "plugins": {"slots": {"memory": "memory-core"}, "entries": {"active-memory": {"enabled": True}}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_discovery_doctor_is_read_only(tmp_path, monkeypatch):
    root = _source(tmp_path)
    config = _openclaw(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    report = doctor(source=str(root), openclaw_config=str(config))
    assert report["source"]["discovered"] is True
    assert report["build"]["dist"] is False
    assert report["openclaw"]["slot"] == "memory-core"
    assert json.loads(config.read_text(encoding="utf-8"))["plugins"]["slots"]["memory"] == "memory-core"


def test_configure_persists_user_source_only(tmp_path, monkeypatch):
    root = _source(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = configure(source=str(root))
    assert result["ok"] is True
    assert json.loads((tmp_path / "config" / "repository-memory" / "config.json").read_text())["memos"]["source_root"] == str(root)


def test_plugin_state_distinguishes_slot_and_install(tmp_path):
    config = {"plugins": {"slots": {"memory": "memory-core"}, "entries": {}}}
    state = _plugin_state(config, tmp_path / "openclaw.json", None)
    assert state["installed"] is False
    assert state["is_active_memory_slot"] is False


def test_openclaw_update_selects_slot_and_allows_capture(tmp_path):
    config = _openclaw(tmp_path)
    extension = tmp_path / "extensions" / "memos-local-openclaw-plugin"
    extension.mkdir(parents=True)
    result = _update_openclaw(config, extension, ["yaole"])
    value = json.loads(config.read_text(encoding="utf-8"))
    entry = value["plugins"]["entries"]["memos-local-openclaw-plugin"]
    assert result["slot"] == "memos-local-openclaw-plugin"
    assert entry["hooks"]["allowConversationAccess"] is True
    assert value["agents"]["defaults"]["memorySearch"]["enabled"] is False


def test_disable_restores_memory_core_and_keeps_plugin_files(tmp_path, monkeypatch):
    config = _openclaw(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = disable(openclaw_config=str(config))
    value = json.loads(config.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert value["plugins"]["slots"]["memory"] == "memory-core"
    assert value["plugins"]["entries"]["active-memory"]["enabled"] is True
