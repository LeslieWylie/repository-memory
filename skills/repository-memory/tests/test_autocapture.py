#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from autocapture import candidate_markdown, candidate_path, candidate_quality, normalize_turn, should_create_candidate


class AutoCaptureTest(unittest.TestCase):
    def test_normalize_drops_tools_and_redacts_credentials(self):
        turn = normalize_turn({
            "session_id": "session-1",
            "run_id": "run-1",
            "messages": [
                {"role": "system", "content": "hidden instructions"},
                {"role": "user", "content": "Please remember this decision"},
                # Assembled from two literals rather than written whole. The
                # runtime value is unchanged, but the source no longer contains
                # a token-shaped string, so the repository-wide credential scan
                # (tools/scan-tree.sh) does not have to special-case tests/ —
                # and a carve-out for tests/ is precisely how this repository's
                # last leak stayed on the default branch.
                {"role": "tool", "content": "token=sk-" + "abcdefghijklmnop"},
                {"role": "assistant", "content": "已完成配置，api_key=secret-value-that-is-long-enough。"},
            ],
        })
        self.assertEqual([item["role"] for item in turn["messages"]], ["user", "assistant"])
        self.assertIn("[REDACTED_SECRET]", turn["messages"][-1]["content"])
        self.assertNotIn("secret-value", turn["messages"][-1]["content"])

    def test_candidate_is_pending_and_l3_is_not_in_candidate_content(self):
        turn = normalize_turn({
            "session_id": "session-1",
            "run_id": "run-1",
            "messages": [
                {"role": "user", "content": "记录这个决定"},
                {"role": "assistant", "content": "决定：以后所有 repository 查询先做 doctor，再使用 verified citation。"},
            ],
        })
        self.assertTrue(should_create_candidate(turn))
        path = candidate_path(turn)
        content = candidate_markdown(turn, {"l0_verified": True, "record_ids": ["l0-1"]}, {"status": "verified", "count": 1})
        self.assertTrue(path.startswith("autocapture/candidates/"))
        self.assertIn("status: candidate", content)
        self.assertIn("evidence_status: pending", content)
        self.assertNotIn("core/write", content)

    def test_short_non_durable_reply_does_not_create_l2(self):
        turn = normalize_turn({
            "messages": [
                {"role": "user", "content": "ok?"},
                {"role": "assistant", "content": "好的。"},
            ],
        })
        self.assertFalse(should_create_candidate(turn))

    def test_status_acknowledgement_does_not_become_durable_memory(self):
        turn = normalize_turn({
            "messages": [
                {"role": "user", "content": "MR 开了吗？"},
                {"role": "assistant", "content": "已看到，MR !33 已打开，当前对应提交 8c867512，后续等待流水线完成。"},
            ],
        })
        quality = candidate_quality(turn)
        self.assertFalse(quality["eligible"])
        self.assertEqual(quality["reason"], "low_information_acknowledgement")

    def test_short_explicit_blocker_is_still_durable(self):
        turn = normalize_turn({
            "messages": [
                {"role": "user", "content": "现在为什么连不上？"},
                {"role": "assistant", "content": "确认，当前阻塞点是 Mac 未启用远程登录，22 端口超时，不是公钥问题。"},
            ],
        })
        quality = candidate_quality(turn)
        self.assertTrue(quality["eligible"])
        self.assertEqual(quality["reason"], "durable_signal")

    def test_short_root_cause_is_durable_but_unfinished_ack_is_not(self):
        root_cause = normalize_turn({"messages": [
            {"role": "user", "content": "故障原因？"},
            {"role": "assistant", "content": "根因是连接池耗尽；上限从 10 调到 30 后恢复。"},
        ]})
        unfinished = normalize_turn({"messages": [
            {"role": "user", "content": "修好了吗？"},
            {"role": "assistant", "content": "收到，修复尚未完成，正在等待流水线。"},
        ]})
        self.assertTrue(should_create_candidate(root_cause))
        self.assertFalse(should_create_candidate(unfinished))

    def test_recall_injection_is_not_recaptured_as_memory(self):
        turn = normalize_turn({
            "messages": [
                {"role": "user", "content": "<relevant-memories>old memory</relevant-memories>真实问题"},
                {"role": "assistant", "content": "结论\n```python\nsecret_internal_code()\n```\n保留这条结论"},
            ],
        })
        self.assertEqual(turn["messages"][0]["content"], "真实问题")
        self.assertEqual(turn["messages"][1]["content"], "结论\n\n保留这条结论")

    def test_upstream_style_turn_boundary_uses_position_and_timestamp_cursor(self):
        turn = normalize_turn({
            "session_id": "session-1",
            "original_user_message_count": 2,
            "after_timestamp": 1704067200,
            "original_user_text": "用户的原始问题",
            "messages": [
                {"role": "user", "timestamp": 1704067100, "content": "旧问题"},
                {"role": "assistant", "timestamp": 1704067150, "content": "旧回答"},
                {"role": "user", "timestamp": 1704067210, "content": "被 recall 污染的问题"},
                {"role": "assistant", "timestamp": 1704067220, "content": "新的结论：保留这个决定"},
            ],
        })
        self.assertEqual([item["role"] for item in turn["messages"]], ["user", "assistant"])
        self.assertEqual(turn["messages"][0]["content"], "用户的原始问题")
        self.assertIn("新的结论", turn["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
