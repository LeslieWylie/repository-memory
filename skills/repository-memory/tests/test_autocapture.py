#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from autocapture import candidate_markdown, candidate_path, normalize_turn, should_create_candidate


class AutoCaptureTest(unittest.TestCase):
    def test_normalize_drops_tools_and_redacts_credentials(self):
        turn = normalize_turn({
            "session_id": "session-1",
            "run_id": "run-1",
            "messages": [
                {"role": "system", "content": "hidden instructions"},
                {"role": "user", "content": "Please remember this decision"},
                {"role": "tool", "content": "token=sk-abcdefghijklmnop"},
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

    def test_recall_injection_is_not_recaptured_as_memory(self):
        turn = normalize_turn({
            "messages": [
                {"role": "user", "content": "<relevant-memories>old memory</relevant-memories>真实问题"},
                {"role": "assistant", "content": "结论\n```python\nsecret_internal_code()\n```\n保留这条结论"},
            ],
        })
        self.assertEqual(turn["messages"][0]["content"], "真实问题")
        self.assertEqual(turn["messages"][1]["content"], "结论\n\n保留这条结论")


if __name__ == "__main__":
    unittest.main()
