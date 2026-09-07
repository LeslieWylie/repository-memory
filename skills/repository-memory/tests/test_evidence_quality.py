"""Regressions distilled from live recall, without private corpus fixtures."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import core
from standalone_memory import is_question_turn
from team_memory import SQLiteTeamMemoryBackend
from tokenize_query import query_terms
from fallback import _claim_support, _requested_dated_section_window
from evidence_quality import ownership_supported


def test_embedded_identifier_and_date_survive_chinese_framing():
    assert "orbit-nonexist-907" in query_terms("ORBIT-NONEXIST-907项目最终上线日期是什么？")
    assert "2026-08-18" in query_terms("2026年8月18日林小明完成了哪些工作？")
    assert "h3" in query_terms("H3现在支持什么？")


def test_question_with_output_instructions_is_not_evidence():
    assert is_question_turn("user", "请用记忆工具回答：SWIFT-826-P9 默认交付时间是什么？只给结论和记忆层级，不要凭常识猜。")
    assert is_question_turn("user", "林小明最近在做什么？请用仓库查一下，给出引用。")
    assert is_question_turn("user", "使用 memory_search 查证：SWIFT-826-P9 默认哪天几点交付？只回答时间和来源层。")
    assert not is_question_turn("user", "请记住：SWIFT-826-P9 默认每周四下午三点交付。")
    assert not is_question_turn("assistant", "发布流程包含验收环节。")


def test_direct_coverage_cannot_override_a_missing_project_identifier():
    item = {
        "excerpt": "没有证据，无法确认 NEBULA-FAKE-405 的最终上线日期。",
        "memory": {"role": "assistant"},
        "support": {"claim_support": "direct", "coverage": 1, "matched_terms": ["最终", "上线", "日期"], "unmatched_terms": []},
    }
    assert core._answerable_items([item], "ORBIT-NONEXIST-907项目最终上线日期是什么？") == []


def test_old_abstention_does_not_answer_even_the_same_project():
    item = {
        "excerpt": "没有证据，无法确认 NEBULA-FAKE-405 的最终上线日期。",
        "memory": {"role": "assistant"},
        "support": {"claim_support": "direct", "coverage": 1},
    }
    assert core._answerable_items([item], "NEBULA-FAKE-405 的上线日期？") == []


def test_recorder_and_tool_use_do_not_establish_a_teams_work():
    query = "Orbit 团队本周完成了什么？"
    record = {"content": "林小明与 Codex 完成视频验证，并通过 Orbit 投递日报。", "attribution": {"recorded_by": "Orbit", "work_performed_by": []}}
    assert not ownership_supported(query, record)
    assert ownership_supported(query, {"content": "Orbit 团队完成了任务调度器的修复。"})
    assert ownership_supported(query, {"content": "完成了任务调度器修复。", "attribution": {"work_performed_by": ["Orbit"]}})


def test_short_model_name_is_still_a_required_claim():
    support = _claim_support(["h3", "验证"], "验证通过。", 1, 1)
    assert support["claim_support"] != "direct"
    assert "h3" in support["unmatched_terms"]


def test_requested_date_stays_inside_its_section():
    text = "## 2026-08-19\n完成缓存调试。\n## 2026-08-18\n完成了发布验证。\n## 2026-08-17\n旧版本仍然失败。"
    excerpt, start, end = _requested_dated_section_window(text, ["发布", "2026-08-18"], ["2026-08-18"])
    assert "2026-08-18" in excerpt and "发布验证" in excerpt
    assert "2026-08-17" not in excerpt and "缓存" not in excerpt
    assert text.splitlines()[start - 1:end] == excerpt.splitlines()


def test_team_aliases_do_not_fill_the_result_window(tmp_path):
    backend = SQLiteTeamMemoryBackend(tmp_path / "team.sqlite3", node_id="test")
    first = backend.publish({"type": "discovery", "content": "Atlas deployment uses one queue.", "author_agent": "recorder", "idempotency_key": "first"})["memory"]
    backend.activate(first["id"], reviewer="reviewer")
    # Simulate a legacy import. New imports already deduplicate some lineages;
    # old data must be safe to query without a destructive migration.
    conn = backend._connect()
    columns = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    values = list(conn.execute("SELECT * FROM memories WHERE id=?", (first["id"],)).fetchone())
    values[columns.index("id")] = "team:central:alias"
    values[columns.index("idempotency_key")] = "second"
    conn.execute(f"INSERT INTO memories ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
    conn.commit()
    conn.close()
    result = backend.search("Atlas 部署", limit=5)
    assert len(result["active"]) == 1
    assert len(result["active"][0]["aliases"]) == 2
    assert result["active"][0]["attribution"]["recorded_by"] == "recorder"
    assert result["active"][0]["attribution"]["work_performed_by"] == []
    assert backend.health()["quality"]["unique_content_count"] == 1


def test_latest_section_is_topic_specific():
    from fallback import _latest_dated_section_window
    text = "## 2026-09-07\nUnrelated cache work.\n## 2026-09-04\nAtlas validation passed.\n## 2026-08-25\nAtlas validation failed."
    excerpt, start, end = _latest_dated_section_window(text, ["atlas", "validation"], anchors=["atlas"])
    assert "passed" in excerpt and "failed" not in excerpt
    assert text.splitlines()[start - 1:end] == excerpt.splitlines()


def test_endpoint_concept_survives_function_word_filter():
    assert "端到端" in query_terms("Atlas 最新的端到端验证结果是什么？")
    assert "点对点" in query_terms("点对点通信怎么样？")


def test_old_direct_answer_does_not_override_newer_partial_evidence():
    items = [
        {"path": "standup/Alex.md", "evidence_date": "2026-09-04", "support": {"claim_support": "partial", "coverage": .5}},
        {"path": "reports/old.md", "evidence_date": "2026-08-19", "support": {"claim_support": "direct", "coverage": 1}},
    ]
    assert core._answerable_items(items, "Atlas 最新验证结果") == []


def test_capture_keeps_raw_turns_but_only_projects_statements(tmp_path):
    import json
    from standalone_memory import StandaloneMemoryClient
    client = StandaloneMemoryClient(tmp_path / "local.sqlite3")
    source = tmp_path / "session.json"
    source.write_text(json.dumps({"session_id": "capture-example", "messages": [
        {"role": "user", "content": "Atlas 何时交付？只给结论。"},
        {"role": "assistant", "content": "Atlas 每周四交付。"},
        {"role": "assistant", "content": "好的。"},
    ]}, ensure_ascii=False))
    client.ingest(source)
    client.ingest(source)
    conn = client._connect()
    counts = dict(conn.execute("SELECT layer, COUNT(*) FROM records WHERE layer IN ('L0','L1') GROUP BY layer"))
    assert counts == {"L0": 3, "L1": 1}
    metadata = json.loads(conn.execute("SELECT metadata FROM records WHERE layer='L1'").fetchone()[0])
    assert metadata["fact_verified"] is False and len(metadata["source_record_ids"]) == 1
    conn.close()
