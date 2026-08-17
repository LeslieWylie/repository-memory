from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from benchmark import run_benchmark
from provider_protocol import manifest, normalize_response
from supervisor import supervise
from team_memory import team_memory_store


class DeliveryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_env = os.environ.copy()
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        os.environ["XDG_CACHE_HOME"] = str(root / "cache")
        os.environ["REPOSITORY_MEMORY_CONFIG"] = str(root / "config.json")
        self.repo = root / "knowledge"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "README.md").write_text("citation-first repository memory\n", encoding="utf-8")
        (self.repo / "docs" / "architecture.md").write_text("MCP transport and separate memory groups\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def test_provider_contract_is_stable(self) -> None:
        self.assertEqual(manifest("fixture", ["search", "doctor"])["capabilities"], ["doctor", "search"])
        value = normalize_response({"results": []}, operation="search", provider="fixture")
        self.assertEqual(value["provider"], "fixture")
        self.assertFalse(value["canonical_repo_changed"])

    def test_supervisor_holds_without_model_and_applies_with_explicit_model(self) -> None:
        candidate = team_memory_store().publish({
            "type": "solution",
            "title": "A reusable repository memory solution",
            "content": "A reusable solution with a checked source citation.",
            "provenance": {"agent": "test", "citations": ["README.md"], "commits": ["fixture"]},
            "confidence": 0.9,
        })["memory"]
        held = supervise(lane="team")
        self.assertEqual(held["accepted"], 0)
        self.assertTrue(held["model_configured"] is False)
        command = [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print(json.dumps({'decision':'accept','confidence':0.95,'model':'fixture-supervisor'}))"]
        applied = supervise(lane="team", apply=True, reviewer="reviewer", command=command)
        self.assertEqual(applied["accepted"], 1)
        self.assertEqual(team_memory_store().get(candidate["id"])["result"]["status"], "active")

    def test_public_benchmark_uses_supplied_qrels(self) -> None:
        queries = Path(__file__).resolve().parents[3] / "eval" / "public" / "queries.jsonl"
        qrels = Path(__file__).resolve().parents[3] / "eval" / "public" / "qrels.jsonl"
        report = run_benchmark(suite="public", root=self.repo, queries=queries, qrels=qrels)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["report"]["qrels_audit"]["ok"])

if __name__ == "__main__":
    unittest.main()
