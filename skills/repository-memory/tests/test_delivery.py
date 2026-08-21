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
        # Every path the public qrels cite has to exist here: the audit this
        # test asserts on resolves gold paths against the repository it is
        # given, so the fixture's file set is a function of the gold set, not
        # of what any single query needs to retrieve.
        (self.repo / "README.md").write_text("citation-first repository memory\nmemory_publish and memory_supersede lifecycle\n", encoding="utf-8")
        (self.repo / "docs" / "architecture.md").write_text("MCP transport and separate memory groups\n", encoding="utf-8")
        (self.repo / "docs" / "quickstart.md").write_text("publish reusable team knowledge and supersede an outdated record\n", encoding="utf-8")
        (self.repo / "CHANGELOG.md").write_text("guard audit is the default and enforce is opt-in\nsemantic configure --provider and --dimensions\nGitHub Actions coverage for Python 3.10, 3.12, and 3.13\n", encoding="utf-8")
        (self.repo / "SECURITY.md").write_text("the OpenClaw guard is audit-first by default\n", encoding="utf-8")
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

    def test_supervisor_accepts_memory_lineage_and_holds_untraceable(self) -> None:
        # Auto-captured team candidates carry a memory lineage, not a Git
        # citation.  Traceability is what the provenance gate is for, so the
        # lineage must be reviewable -- and a record with neither a citation
        # nor a lineage must hold even when the model says accept.
        store = team_memory_store()
        lineage = store.publish({
            "type": "discovery",
            "title": "A reusable discovery captured from a turn",
            "content": "A concrete discovery another agent can act on, extracted from a bounded turn.",
            "provenance": {"agent_id": "yaole", "source_memory_id": "l1-0001", "observed_at": "2026-08-19T00:00:00+00:00", "run_id": "run-1"},
            "confidence": 0.8,
        })["memory"]
        untraceable = store.publish({
            "type": "discovery",
            "title": "A record that cannot say where it came from",
            "content": "Plausible text with no citation and no memory lineage behind it.",
            "provenance": {"agent_id": "yaole"},
            "confidence": 0.8,
        })["memory"]
        command = [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print(json.dumps({'decision':'accept','confidence':0.95,'model':'fixture-supervisor'}))"]
        applied = supervise(lane="team", apply=True, reviewer="reviewer", command=command)
        by_id = {receipt["id"]: receipt for receipt in applied["receipts"]}
        self.assertEqual(by_id[lineage["id"]]["decision"], "accept")
        self.assertEqual(by_id[lineage["id"]]["checks"]["provenance_kind"], "memory-lineage")
        self.assertEqual(store.get(lineage["id"])["result"]["status"], "active")
        self.assertEqual(by_id[untraceable["id"]]["decision"], "hold")
        self.assertEqual(by_id[untraceable["id"]]["checks"]["provenance_kind"], "none")
        self.assertEqual(store.get(untraceable["id"])["result"]["status"], "candidate")

    def test_activation_reaches_every_projection_of_the_memory(self) -> None:
        # A pull hydrates the same memory back into the store as a central
        # wrapper linked by provenance.source_memory_id.  Activating either
        # projection must activate both, or the exporter -- which prefers the
        # local original -- keeps publishing candidate state forever.
        store = team_memory_store()
        original = store.publish({
            "type": "solution",
            "title": "A reusable solution that was hydrated back",
            "content": "A concrete reusable solution with enough content to review and act on.",
            "provenance": {"agent_id": "yaole", "citations": ["README.md"]},
            "confidence": 0.8,
        })["memory"]
        wrapper = store.publish({
            "id": "team:central:team_l1_00000000000000000000feed",
            "type": "solution",
            "title": "A reusable solution that was hydrated back",
            "content": "A concrete reusable solution with enough content to review and act on.",
            "provenance": {"agent_id": "yaole", "central_id": "team_l1_00000000000000000000feed", "source_memory_id": original["id"]},
            "confidence": 0.8,
        })["memory"]
        result = store.activate(wrapper["id"], reviewer="reviewer")
        self.assertEqual(result["status"], "active")
        self.assertIn(original["id"], result["activated_siblings"])
        self.assertEqual(store.get(original["id"])["result"]["status"], "active")
        self.assertEqual(store.get(original["id"])["result"]["reviewed_by"], "reviewer")


    def test_skill_frontmatter_version_matches_the_release(self) -> None:
        # The Skill frontmatter now carries metadata.version per the open
        # Agent Skills spec (version is NOT a top-level key there). Hosts read
        # the copy, not the repo -- a stale frontmatter version would tell
        # every host it runs an older release than the runtime underneath it.
        import re
        root = Path(__file__).resolve().parents[1]
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        front = skill.split("---")[1]
        match = re.search(r"version:\s*\"([^\"]+)\"", front)
        assert match is not None
        version_file = (root / "VERSION").read_text(encoding="utf-8").strip()
        assert match.group(1) == version_file
        pyproject = (root.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        assert f'version = "{version_file}"' in pyproject

    def test_public_benchmark_uses_supplied_qrels(self) -> None:
        queries = Path(__file__).resolve().parents[3] / "eval" / "public" / "queries.jsonl"
        qrels = Path(__file__).resolve().parents[3] / "eval" / "public" / "qrels.jsonl"
        report = run_benchmark(suite="public", root=self.repo, queries=queries, qrels=qrels)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["report"]["qrels_audit"]["ok"])

if __name__ == "__main__":
    unittest.main()
