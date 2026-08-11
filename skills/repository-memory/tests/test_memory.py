#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import core
from citation import locate, validate
from evaluate import evaluate_queries
from fallback import paths, query_terms
from memorycore import MemoryCoreClient, MemoryCoreConfig
from mcp_server import SERVER_VERSION
from snapshot import _snapshot_lock, prepare_view, snapshot_lock_backend
from team_memory import TeamMemoryStore
from version import VERSION

from models import SourceSpec

FAKE_ADAPTER = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
source = os.environ.get("REPOSITORY_MEMORY_SOURCE_ID", "source")
commit = os.environ.get("REPOSITORY_MEMORY_SOURCE_COMMIT", "unknown")
if args and args[0] == "doctor":
    print(json.dumps({"capabilities": ["doctor", "sync", "search", "get"], "indexed_commit": commit, "registered": True}))
elif args and args[0] == "sync":
    print(json.dumps({"synced": True, "indexed_commit": commit, "source": source}))
elif args and args[0] == "search":
    query = args[args.index("--query") + 1] if "--query" in args else ""
    pending = "pending" in query.lower()
    path = "docs/pending.md" if pending else ("docs/beta.md" if source == "beta" else "docs/atlas.md")
    status = "pending" if pending else "secondary"
    print(json.dumps({"results": [{
        "id": source + ":" + path,
        "title": path,
        "path": path,
        "snippet": "Pending candidate" if pending else "Atlas evidence from " + source,
        "evidence_status": status,
        "citation": {"path": path, "commit": commit, "memory_id": source + ":" + path, "locator": {"start_line": 2, "end_line": 2}}
    }]}))
elif args and args[0] == "get":
    value = args[args.index("--id") + 1] if "--id" in args else ""
    print(json.dumps({"id": value, "path": value.split(":", 1)[-1], "content": "Atlas evidence"}))
else:
    print(json.dumps({"error": "unsupported fake adapter command", "args": args}))
    raise SystemExit(2)
'''


class RepositoryMemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.config = base / "config.json"
        self.data = base / "data"
        self.cache = base / "cache"
        self.old_env = os.environ.copy()
        os.environ.update({
            "XDG_DATA_HOME": str(self.data),
            "XDG_CACHE_HOME": str(self.cache),
            "REPOSITORY_MEMORY_CONFIG": str(self.config),
        })
        self.alpha = self.make_repo("alpha", "Atlas evidence from alpha\n")
        self.beta = self.make_repo("beta", "Atlas evidence from beta\n")
        self.adapter = base / "fake-adapter.py"
        self.adapter.write_text(FAKE_ADAPTER, encoding="utf-8")
        self.adapter.chmod(self.adapter.stat().st_mode | stat.S_IXUSR)
        self.write_config({
            "sources": [
                {"id": "alpha", "root": str(self.alpha), "adapter": str(self.adapter)},
                {"id": "beta", "root": str(self.beta), "adapter": str(self.adapter)},
            ]
        })

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def make_repo(self, name: str, content: str) -> Path:
        root = Path(self.temp.name) / name
        root.mkdir()
        (root / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / ("beta.md" if name == "beta" else "atlas.md")).write_text("# Record\n" + content, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True)
        return root

    def write_config(self, value: dict):
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def test_runtime_version_comes_from_skill_version_file(self):
        self.assertEqual(SERVER_VERSION, VERSION)
        self.assertEqual(VERSION, (SCRIPTS.parent / "VERSION").read_text(encoding="utf-8").strip())
        self.assertEqual(VERSION, "0.2.0")

    def test_multisource_search_has_verified_and_candidates(self):
        result = core.search(None, "Atlas evidence", limit=5)
        self.assertFalse(result["abstain"])
        self.assertEqual({item["source"] for item in result["verified"]}, {"alpha", "beta"})
        self.assertEqual(result["verified"][0]["citation"]["valid"], True)
        self.assertEqual(result["results"], result["verified"])

        pending = core.search(None, "pending candidate", limit=5)
        self.assertTrue(pending["abstain"])
        self.assertEqual(len(pending["verified"]), 0)
        self.assertEqual(pending["candidates"][0]["evidence_status"], "pending")

    def test_negative_and_local_structured_backend_are_conservative(self):
        negative = core.search(None, "fictional benchmark ZZZQWE")
        self.assertTrue(negative["abstain"])
        self.assertEqual(negative["verified"], [])
        translated_negative = core.search(None, "ZZZQWE project recent progress")
        self.assertTrue(translated_negative["abstain"])
        self.assertEqual(translated_negative["verified"], [])

        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})
        result = core.search(None, "Atlas evidence", local=True)
        self.assertFalse(result["abstain"])
        self.assertEqual(result["diagnostics"]["adapters"][0]["adapter"], "repository-local-structured")
        self.assertEqual(result["diagnostics"]["adapters"][0]["fallback"], False)
        self.assertEqual(result["verified"][0]["citation"]["source"], "repository")

        (self.alpha / ".env").write_text("TOKEN=do-not-index\n", encoding="utf-8")
        (self.alpha / ".env").chmod(0o600)
        dirty_result = core.search(None, "Atlas evidence", local=True)
        self.assertTrue(dirty_result["abstain"])
        self.assertTrue(all(".env" not in item["path"] for item in dirty_result["candidates"]))

    def test_local_only_source_is_fresh_without_remote_fetch(self):
        self.write_config({
            "sources": [{"id": "alpha", "root": str(self.alpha), "local_only": True}],
        })

        result = core.search(None, "Atlas evidence")

        self.assertFalse(result["abstain"])
        self.assertEqual(result["verified"][0]["citation"]["valid"], True)
        self.assertEqual(result["freshness"]["alpha"]["state"], "fresh")
        self.assertEqual(result["freshness"]["alpha"]["commit_type"], "local_worktree")
        self.assertIsNone(result["freshness"]["alpha"]["fetch_error"])

    def test_default_index_filters_operational_templates_but_deep_can_include_them(self):
        (self.alpha / "templates").mkdir()
        (self.alpha / "templates" / "record-template.md").write_text("# Template\n", encoding="utf-8")
        (self.alpha / "logs").mkdir()
        (self.alpha / "logs" / "run.md").write_text("# Run log\n", encoding="utf-8")
        normal = paths(self.alpha, deep=False)
        deep = paths(self.alpha, deep=True)
        self.assertNotIn("templates/record-template.md", normal)
        self.assertNotIn("logs/run.md", normal)
        self.assertIn("templates/record-template.md", deep)
        self.assertIn("logs/run.md", deep)

    def test_natural_cjk_temporal_question_retrieves_named_standup(self):
        standup = self.alpha / "standup"
        standup.mkdir()
        (standup / "武垚乐.md").write_text(
            "# 武垚乐\n\n## 2026-08-07\n\n推进 repository-memory MCP 接入和日志分析流水线。\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.alpha), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "standup"], check=True)
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})

        result = core.search(None, "武垚乐最近在干啥", local=True)

        self.assertFalse(result["abstain"])
        self.assertEqual(result["mode"], "temporal")
        self.assertEqual(result["verified"][0]["path"], "standup/武垚乐.md")
        self.assertEqual(result["verified"][0]["citation"]["valid"], True)
        self.assertIn("武垚乐", result["diagnostics"]["query_terms"])
        self.assertFalse(result["freshness"]["alpha"]["dirty"])

    def test_cjk_temporal_scaffolding_does_not_become_query_terms(self):
        terms = query_terms("最近的模型评审")
        self.assertIn("模型评审", terms)
        self.assertIn("模型", terms)
        self.assertIn("评审", terms)
        self.assertNotIn("最近的", terms)
        self.assertNotIn("的模型", terms)

    def test_temporal_question_without_entity_uses_personal_layer_when_available(self):
        standup = self.alpha / "standup"
        standup.mkdir()
        (standup / "daily.md").write_text(
            "# Daily\n\n## 2026-08-07\n\n完成今日工作记录。\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.alpha), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "daily"], check=True)
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})

        result = core.search(None, "你最近在干什么", local=True)

        self.assertFalse(result["abstain"])
        self.assertEqual(result["mode"], "temporal")
        self.assertEqual(result["verified"][0]["path"], "standup/daily.md")

    def test_remote_snapshot_does_not_use_dirty_worktree(self):
        bare = Path(self.temp.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "remote", "add", "origin", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "push", "-q", "-u", "origin", "main"], check=True)
        (self.alpha / "docs" / "atlas.md").write_text("# Local uncommitted secret\n", encoding="utf-8")
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True).strip()
        view = prepare_view(SourceSpec("alpha", self.alpha, "alpha"))
        self.assertEqual(view.commit_type, "remote_snapshot")
        self.assertEqual(view.commit, commit)
        self.assertNotEqual(view.path, self.alpha)
        self.assertFalse(view.dirty)
        self.assertIn("Atlas evidence", (view.path / "docs" / "atlas.md").read_text(encoding="utf-8"))
        self.assertIn("Local uncommitted", (self.alpha / "docs" / "atlas.md").read_text(encoding="utf-8"))

    def test_doctor_reports_effective_remote_snapshot_not_dirty_worktree(self):
        bare = Path(self.temp.name) / "doctor-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "remote", "add", "origin", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "push", "-q", "-u", "origin", "main"], check=True)
        (self.alpha / "docs" / "atlas.md").write_text("# Dirty local edit\n", encoding="utf-8")
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})

        report = core.doctor(None)
        source = report["sources"][0]
        self.assertEqual(source["freshness"]["commit_type"], "remote_snapshot")
        self.assertFalse(source["freshness"]["dirty"])
        self.assertTrue(source["state"]["dirty"])

    def test_shared_snapshot_lock_serializes_concurrent_clients(self):
        if snapshot_lock_backend() == "unavailable":
            self.skipTest("snapshot locking backend unavailable")
        target = Path(self.temp.name) / "shared-snapshot"
        holder_entered = threading.Event()
        release_holder = threading.Event()
        waiter_entered = threading.Event()

        def hold_lock():
            with _snapshot_lock(target, timeout=2):
                holder_entered.set()
                release_holder.wait(2)

        def wait_for_lock():
            with _snapshot_lock(target, timeout=2):
                waiter_entered.set()

        holder = threading.Thread(target=hold_lock)
        waiter = threading.Thread(target=wait_for_lock)
        holder.start()
        self.assertTrue(holder_entered.wait(1))
        waiter.start()
        time.sleep(0.1)
        self.assertFalse(waiter_entered.is_set())
        release_holder.set()
        holder.join(2)
        waiter.join(2)
        self.assertTrue(waiter_entered.is_set())

    def test_doctor_writes_and_parser(self):
        report = core.doctor(None)
        self.assertEqual({item["source"] for item in report["sources"]}, {"alpha", "beta"})
        self.assertTrue(all(item["healthy"] for item in report["sources"]))
        self.assertTrue(all(item["index"]["exists"] for item in report["sources"]))
        self.assertTrue(all(item["index"]["document_count"] > 0 for item in report["sources"]))
        before = core.build_parser().parse_args(["--root", str(self.alpha), "doctor", "--json"])
        after = core.build_parser().parse_args(["doctor", "--root", str(self.alpha), "--json"])
        self.assertEqual(before.root, str(self.alpha))
        self.assertEqual(after.root, str(self.alpha))

        before_status = subprocess.check_output(["git", "-C", str(self.alpha), "status", "--porcelain"], text=True)
        feedback = core.feedback(self.alpha, "alpha:docs/atlas.md", "useful", "up")
        candidate_input = Path(self.temp.name) / "candidate.json"
        candidate_input.write_text(json.dumps({"title": "Candidate", "content": "Pending evidence"}), encoding="utf-8")
        promoted = core.promote(self.alpha, str(candidate_input))
        after_status = subprocess.check_output(["git", "-C", str(self.alpha), "status", "--porcelain"], text=True)
        self.assertTrue(feedback["written"])
        self.assertEqual(promoted["status"], "candidate")
        self.assertFalse(promoted["canonical_repo_changed"])
        self.assertEqual(before_status, after_status)

    def test_shared_team_memory_context_lifecycle_and_feedback(self):
        record_input = Path(self.temp.name) / "team-memory.json"
        record_input.write_text(json.dumps({
            "type": "decision",
            "title": "Use isolated worktrees",
            "content": "Each issue uses an isolated persistent worktree; do not work from the canonical clone.",
            "scope": {"repo": "alpha", "issue": "A-42"},
            "provenance": {"agent": "planner", "commits": ["abc1234"]},
            "confidence": 0.9,
            "status": "active",
        }), encoding="utf-8")
        published = core.publish_memory(str(record_input), status="active")
        memory = published["published"][0]["memory"]
        self.assertTrue(memory["id"].startswith("team:decision:"))
        context = core.memory_context(None, "isolated worktree Atlas evidence", repo="alpha")
        self.assertTrue(context["context"]["repository_evidence"])
        self.assertEqual(context["context"]["decisions"][0]["id"], memory["id"])
        self.assertEqual(context["retrieval_mode"], "multi-source-lexical")
        self.assertTrue(context["diagnostics"]["parallel_recall"])
        self.assertFalse(context["semantic_available"])

        feedback = core.feedback(None, memory["id"], "reused successfully", "helpful")
        self.assertTrue(feedback["ok"])
        replacement_input = Path(self.temp.name) / "replacement.json"
        replacement_input.write_text(json.dumps({
            "type": "decision",
            "title": "Use isolated worktrees, updated",
            "content": "Keep one isolated worktree per issue and record the branch in the handoff.",
            "scope": {"repo": "alpha", "issue": "A-42"},
            "provenance": {"agent": "reviewer", "commits": ["def5678"]},
            "confidence": 0.95,
        }), encoding="utf-8")
        superseded = core.supersede_memory(memory["id"], str(replacement_input))
        replacement_id = superseded["replacement"]["memory"]["id"]
        self.assertEqual(core.get_result(None, memory["id"])["result"]["status"], "superseded")
        self.assertEqual(core.get_result(None, replacement_id)["result"]["status"], "active")

    def test_team_memory_expiry_and_feedback_update_lifecycle(self):
        store = TeamMemoryStore(Path(self.temp.name) / "expiry.sqlite3")
        expired = store.publish({
            "type": "decision",
            "title": "Temporary decision",
            "content": "Use the temporary runner until the migration finishes.",
            "status": "active",
            "valid_until": "2000-01-01T00:00:00+00:00",
        }, default_status="active")
        memory_id = expired["memory"]["id"]
        search = store.search("temporary runner")
        self.assertEqual(search["active"], [])
        self.assertEqual(search["diagnostics"]["expired_count"], 1)
        self.assertTrue(store.get(memory_id)["result"]["expired"])
        self.assertEqual(store.feedback(memory_id, "wrong", "review rejected", agent="reviewer")["status"], "stale")

        fresh = store.publish({
            "type": "handoff",
            "title": "Fresh handoff",
            "content": "The isolated worktree is ready for the next agent.",
            "status": "active",
        }, default_status="active")
        fresh_id = fresh["memory"]["id"]
        self.assertEqual(store.feedback(fresh_id, "stale", "old from my lane", agent="agent-a")["status"], "active")
        self.assertEqual(store.feedback(fresh_id, "stale", "confirmed old", agent="agent-b")["status"], "stale")

    def test_team_memory_concurrent_writers_and_bundle_round_trip(self):
        path = Path(self.temp.name) / "shared.sqlite3"

        def publish(index: int):
            return TeamMemoryStore(path).publish({
                "type": "discovery",
                "title": f"Concurrent discovery {index}",
                "content": f"Agent {index} found the shared writer path.",
                "status": "active",
                "idempotency_key": f"concurrent-{index}",
                "author_agent": f"agent-{index}",
            }, default_status="active")

        errors = []
        results = []

        def worker(index: int):
            try:
                results.append(publish(index))
            except Exception as exc:  # pragma: no cover - failure is asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 20)
        self.assertEqual(TeamMemoryStore(path).health()["record_count"], 20)

        os.environ["REPOSITORY_MEMORY_TEAM_DB"] = str(path)
        bundle_path = Path(self.temp.name) / "team-bundle.json"
        exported = core.export_team_memory(str(bundle_path))
        self.assertEqual(exported["records"], 20)
        imported_db = Path(self.temp.name) / "imported.sqlite3"
        os.environ["REPOSITORY_MEMORY_TEAM_DB"] = str(imported_db)
        imported = core.import_team_memory(str(bundle_path))
        self.assertEqual(imported["imported"]["inserted"], 20)
        self.assertEqual(TeamMemoryStore(imported_db).health()["record_count"], 20)

    def test_team_memory_public_benchmark_is_isolated_and_measures_top1(self):
        from team_memory_eval import evaluate_team_memory

        root = Path(__file__).resolve().parents[3]
        report = evaluate_team_memory(
            root / "eval/public/team_memory/records.jsonl",
            root / "eval/public/team_memory/queries.jsonl",
            root / "eval/public/team_memory/qrels.jsonl",
        )
        self.assertEqual(report["metrics"]["precision_at_1"], 1.0)
        self.assertEqual(report["metrics"]["recall_at_5"], 1.0)
        self.assertEqual(report["metrics"]["negative_abstain_accuracy"], 1.0)
        self.assertFalse(report["canonical_repo_changed"])


    def test_mcp_stdio_matches_cli_contract(self):
        command = [sys.executable, str(SCRIPTS / "repository-memory.py"), "mcp", "--root", str(self.alpha)]
        modern_meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": modern_meta}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": modern_meta}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"_meta": modern_meta, "name": "memory_search", "arguments": {"query": "Atlas evidence"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"_meta": modern_meta, "name": "not-a-memory-tool", "arguments": {"source": "alpha"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"_meta": modern_meta, "name": "memory_context", "arguments": {"query": "Atlas evidence"}}},
        ]
        process = subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, capture_output=True, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertIn("2026-07-28", responses[0]["result"]["supportedVersions"])
        self.assertEqual(responses[0]["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "repository-memory")
        self.assertEqual(responses[0]["result"]["resultType"], "complete")
        self.assertEqual(responses[1]["result"]["resultType"], "complete")
        self.assertEqual({tool["name"] for tool in responses[1]["result"]["tools"]}, {"memory_doctor", "memory_sync", "memory_search", "memory_get", "memory_init", "memory_ingest", "memory_context", "memory_team_sync", "memory_publish", "memory_feedback", "memory_supersede"})
        payload = responses[2]["result"]["structuredContent"]
        self.assertEqual(responses[2]["result"]["resultType"], "complete")
        self.assertIn("verified", payload)
        self.assertIn("candidates", payload)
        self.assertFalse(payload["abstain"])
        self.assertEqual({item["source"] for item in payload["verified"]}, {"alpha"})
        context_payload = responses[4]["result"]["structuredContent"]
        self.assertIn("repository_evidence", context_payload["context"])
        self.assertEqual(context_payload["semantic_available"], False)
        error_data = responses[3]["error"]["data"]
        self.assertEqual(error_data["adapter"], "repository-memory-runtime")
        self.assertEqual(error_data["source"], "alpha")
        self.assertIn("freshness", error_data)

    def test_mcp_legacy_initialize_remains_compatibility_only(self):
        command = [sys.executable, str(SCRIPTS / "repository-memory.py"), "mcp", "--root", str(self.alpha)]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        process = subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, capture_output=True, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[1]["result"]["resultType"], "complete")

    def test_mcp_rejects_unknown_per_request_protocol(self):
        command = [sys.executable, str(SCRIPTS / "repository-memory.py"), "mcp", "--root", str(self.alpha)]
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}}}
        process = subprocess.run(command, input=json.dumps(request) + "\n", text=True, capture_output=True, check=True)
        response = json.loads(process.stdout.strip())
        self.assertEqual(response["error"]["code"], -32022)
        self.assertIn("2026-07-28", response["error"]["data"]["supported"])

    def test_audit_proxy_forwards_mcp_and_logs_metadata_only(self):
        audit_log = Path(self.temp.name) / "audit.jsonl"
        command = [
            sys.executable,
            str(SCRIPTS / "audit_proxy.py"),
            "--log",
            str(audit_log),
            "--",
            sys.executable,
            str(SCRIPTS / "repository-memory.py"),
            "mcp",
            "--root",
            str(self.alpha),
        ]
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"_meta": meta, "name": "memory_search", "arguments": {"query": "Atlas evidence"}}},
        ]
        process = subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, capture_output=True, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual(len(responses), 3)
        self.assertIn("verified", responses[2]["result"]["structuredContent"])
        events = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len([event for event in events if event["direction"] == "request"]), 3)
        self.assertEqual(len([event for event in events if event["direction"] == "response"]), 3)
        search_request = next(event for event in events if event["direction"] == "request" and event.get("tool") == "memory_search")
        search_response = next(event for event in events if event["direction"] == "response" and event.get("tool") == "memory_search")
        self.assertTrue(search_request["query_sha256"])
        self.assertEqual(search_request["protocol_version"], "2026-07-28")
        self.assertTrue(search_request["modern_protocol"])
        self.assertNotIn("Atlas evidence", audit_log.read_text(encoding="utf-8"))
        self.assertEqual(search_response["verified_count"], 1)
        self.assertEqual(search_response["protocol_version"], "2026-07-28")

    def test_audit_proxy_labels_legacy_host_after_negotiation(self):
        audit_log = Path(self.temp.name) / "legacy-audit.jsonl"
        command = [
            sys.executable,
            str(SCRIPTS / "audit_proxy.py"),
            "--log",
            str(audit_log),
            "--",
            sys.executable,
            str(SCRIPTS / "repository-memory.py"),
            "mcp",
            "--root",
            str(self.alpha),
        ]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, capture_output=True, check=True)
        events = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
        tools_list = next(event for event in events if event["direction"] == "request" and event["method"] == "tools/list")
        tools_response = next(event for event in events if event["direction"] == "response" and event["id"] == "2")
        self.assertEqual(tools_list["protocol_version"], "2025-11-25")
        self.assertFalse(tools_list["modern_protocol"])
        self.assertEqual(tools_response["protocol_version"], "2025-11-25")

    def test_document_qrels_evaluator_only_credits_verified_top1(self):
        queries = Path(self.temp.name) / "queries.jsonl"
        qrels = Path(self.temp.name) / "qrels.jsonl"
        queries.write_text(
            "\n".join([
                json.dumps({"id": "q1", "query": "Atlas evidence", "intent": "exact", "quality": "focused"}),
                json.dumps({"id": "q2", "query": "fictional benchmark ZZZQWE", "intent": "negative", "quality": "focused", "expected_abstain": True}),
            ]) + "\n",
            encoding="utf-8",
        )
        qrels.write_text(
            json.dumps({
                "query_id": "q1",
                "document_id": "alpha:docs/atlas.md",
                "source": "alpha",
                "path": "docs/atlas.md",
                "relevance": 2,
            }) + "\n",
            encoding="utf-8",
        )
        os.environ["REPOSITORY_MEMORY_DISABLE_ADAPTER"] = "1"
        report = evaluate_queries(self.alpha, queries, qrels, local=True)
        self.assertTrue(report["qrels_audit"]["ok"])
        self.assertEqual(report["precision_at_1"], 1.0)
        self.assertEqual(report["strict_precision_at_1"], 1.0)
        self.assertEqual(report["negative_abstain_accuracy"], 1.0)
        self.assertEqual(report["query_quality"]["quality_counts"]["focused"], 2)

    def test_evaluator_recall_counts_multiple_gold_documents_and_pins_citation_commit(self):
        queries = Path(self.temp.name) / "multi-gold-queries.jsonl"
        qrels = Path(self.temp.name) / "multi-gold-qrels.jsonl"
        queries.write_text(json.dumps({"id": "q1", "query": "Atlas evidence", "intent": "exact", "quality": "focused"}) + "\n", encoding="utf-8")
        qrels.write_text(
            "\n".join([
                json.dumps({"query_id": "q1", "document_id": "alpha:docs/atlas.md", "source": "alpha", "path": "docs/atlas.md", "relevance": 2}),
                json.dumps({"query_id": "q1", "document_id": "alpha:README.md", "source": "alpha", "path": "README.md", "relevance": 1}),
            ]) + "\n",
            encoding="utf-8",
        )
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True).strip()
        verified = {
            "id": "alpha:docs/atlas.md",
            "citation": {"valid": True, "source": "repository", "path": "docs/atlas.md", "line_start": 1, "line_end": 2, "commit": commit},
        }
        with patch("evaluate.search", return_value={"verified": [verified], "candidates": [], "abstain": False, "mode": "exact"}):
            report = evaluate_queries(self.alpha, queries, qrels, local=True)
        self.assertEqual(report["precision_at_1"], 1.0)
        self.assertEqual(report["recall_at_5"], 0.5)
        self.assertEqual(report["recall_at_5_micro"], 0.5)
        self.assertEqual(report["citation_parseability"], 1.0)
        mismatched = {**verified, "citation": {**verified["citation"], "commit": "wrong"}}
        with patch("evaluate.search", return_value={"verified": [mismatched], "candidates": [], "abstain": False, "mode": "exact"}):
            mismatched_report = evaluate_queries(self.alpha, queries, qrels, local=True)
        self.assertEqual(mismatched_report["citation_parseability"], 0.0)

    def test_memory_layer_metadata_is_preserved_but_citation_still_controls_verification(self):
        view = prepare_view(SourceSpec("alpha", self.alpha, "alpha"), local=True)
        payload = {
            "long_term": {
                "query_source": "memorycore",
                "strategy": "keyword",
                "results": [{
                    "id": "atomic-1",
                    "memory_layer": "L1",
                    "memory_type": "atomic",
                    "content": "Atlas evidence from alpha",
                    "citation": {
                        "repository": "alpha",
                        "path": "docs/atlas.md",
                        "commit": view.commit,
                        "locator": {"start_line": 2, "end_line": 2},
                    },
                }],
            }
        }
        item, source = core._raw_results(payload)[0]
        normalized = core.normalize_item(item, view, source)
        self.assertEqual(normalized["memory"], {"layer": "L1", "type": "atomic", "query_source": "memorycore", "strategy": "keyword"})
        self.assertTrue(normalized["citation"]["valid"])
        self.assertEqual(normalized["repository"], "alpha")

    def test_explicit_session_ingest_uses_legacy_adapter_and_reports_layers(self):
        legacy = Path(self.temp.name) / "legacy-adapter.py"
        legacy.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if sys.argv[1:2] == ['ingest-session']:\n"
            "    print(json.dumps({'sessions': 1, 'pipeline': {'skipped': True}}))\n"
            "else:\n"
            "    print(json.dumps({'ok': True}))\n",
            encoding="utf-8",
        )
        legacy.chmod(legacy.stat().st_mode | stat.S_IXUSR)
        self.write_config({
            "backend": {"protocol": "legacy-legacy-memory"},
            "sources": [{"id": "alpha", "root": str(self.alpha), "adapter": str(legacy)}],
        })
        session = Path(self.temp.name) / "session.json"
        session.write_text(json.dumps({"session_id": "s-1", "rounds": [[{"role": "user", "content": "hello"}]]}), encoding="utf-8")
        result = core.ingest_session(self.alpha, str(session), "alpha")
        self.assertTrue(result["ok"])
        self.assertEqual(result["memory"]["supported_layers"], ["L0", "L1", "L2", "L3"])
        self.assertEqual(result["write_operation"], "explicit")
        self.assertFalse(result["canonical_repo_changed"])

    def test_native_memory_search_is_verified_without_git_path(self):
        view = prepare_view(SourceSpec("alpha", self.alpha, "alpha"), local=True)
        config = MemoryCoreConfig(
            endpoint="http://127.0.0.1:8420",
            api_key=None,
            team_id="team",
            agent_id="yaole",
            user_id="user",
        )

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return self.payload

        def fake_urlopen(req, timeout):
            del timeout
            if req.full_url.endswith("/health"):
                return Response({"status": "ok", "version": "test"})
            if req.full_url.endswith("/v3/conversation/search"):
                return Response({"code": 0, "data": {"messages": [{"id": "msg-1", "content": "native evidence", "score": 2.0}]}})
            if req.full_url.endswith("/v3/atomic/search"):
                return Response({"code": 0, "data": {"items": []}})
            if req.full_url.endswith("/v3/scenario/ls"):
                return Response({"code": 0, "data": {"entries": []}})
            if req.full_url.endswith("/v3/core/read"):
                return Response({"code": 0, "data": {"content": ""}})
            raise AssertionError(req.full_url)

        with patch("memorycore.request.urlopen", side_effect=fake_urlopen), patch("memorycore.request.build_opener") as build_opener:
            build_opener.return_value.open.side_effect = fake_urlopen
            client = MemoryCoreClient(config)
            native = client.search("evidence", 5)
            self.assertEqual(native[0]["id"], "memorycore:L0:msg-1")
            normalized = core.normalize_item(native[0], view, "memorycore")
            self.assertTrue(normalized["citation"]["valid"])
            self.assertEqual(normalized["source"], "memorycore")
            self.assertIsNone(normalized["path"])

    def test_memorycore_doctor_probes_each_supported_layer(self):
        config = MemoryCoreConfig(endpoint="http://127.0.0.1:8420", api_key=None, team_id="team", agent_id="agent", user_id="user")
        client = MemoryCoreClient(config)
        responses = iter([
            {"code": 0, "data": {"status": "ok"}},
            {"code": 0, "data": {"messages": [], "total": 0}},
            {"code": 0, "data": {"items": [], "total": 0}},
            {"code": 0, "data": {"entries": [], "total": 0}},
            {"code": 0, "data": {"content": ""}},
        ])
        with patch.object(client, "_request", side_effect=lambda *_args, **_kwargs: next(responses)):
            report = client.health(refresh=True, probe_layers=True)
        self.assertEqual(set(report["layers"]), {"L0", "L1", "L2", "L3"})
        self.assertTrue(all(item["reachable"] for item in report["layers"].values()))

    def test_document_verification_is_independent_from_claim_coverage(self):
        composite = self.alpha / "docs" / "composite.md"
        composite.write_text("# Composite\nAtlas and alpha are introduced here.\n" + ("context line\n" * 14) + "beta is documented in the same record.\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.alpha), "add", str(composite)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "composite"], check=True)
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})
        result = core.search(self.alpha, "Atlas alpha beta", local=True)
        hit = next(item for item in result["verified"] if item["path"] == "docs/composite.md")
        self.assertTrue(result["verified"])
        self.assertEqual(hit["evidence_status"], "secondary")
        self.assertEqual(hit["support"]["claim_support"], "partial")
        self.assertIn("beta", hit["support"]["unmatched_terms"])
        self.assertGreaterEqual(hit["line_end"], hit["line_start"])

    def test_unrelated_adapter_excerpt_cannot_become_verified(self):
        view = prepare_view(SourceSpec("alpha", self.alpha, "alpha"), local=True)
        item = {
            "path": "docs/atlas.md",
            "excerpt": "This text is not present in the cited file.",
            "citation": {"path": "docs/atlas.md", "commit": view.commit, "locator": {"start_line": 1, "end_line": 2}},
        }
        normalized = core.normalize_item(item, view, "adapter")
        self.assertFalse(normalized["citation"]["valid"])
        self.assertEqual(normalized["candidate_reason"], "citation excerpt does not match cited lines")

    def test_citation_accepts_short_exact_excerpt_and_rejects_escape(self):
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True).strip()
        accepted = validate(self.alpha, "docs/atlas.md", 2, 2, "Atlas evidence from alpha", commit, commit)
        self.assertTrue(accepted["valid"])
        short = validate(self.alpha, "docs/atlas.md", 1, 1, "#", commit, commit)
        self.assertTrue(short["valid"])
        escaped = validate(self.alpha, str(self.alpha / "docs" / "atlas.md"), 1, 1, "# Record", commit, commit)
        self.assertFalse(escaped["valid"])
        self.assertEqual(locate(self.alpha, "../README.md", "# alpha"), (None, None))

    def test_multiline_citation_locator_covers_evidence_window(self):
        excerpt = "# Record\nAtlas evidence from alpha"
        start, end = locate(self.alpha, "docs/atlas.md", excerpt)
        self.assertEqual((start, end), (1, 2))
        fetched = core.get_result(self.alpha, "alpha:docs/atlas.md", explain=True)
        self.assertTrue(fetched["found"])
        self.assertEqual(fetched["result"]["citation"]["line_start"], 1)
        self.assertEqual(fetched["result"]["citation"]["line_end"], 2)

    def test_get_can_pin_citation_commit_and_reject_newer_source(self):
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})
        found = core.search(self.alpha, "Atlas evidence", local=True)
        commit = found["verified"][0]["commit"]
        self.assertTrue(core.get_result(self.alpha, "alpha:docs/atlas.md", expected_commit=commit)["found"])
        mismatch = core.get_result(self.alpha, "alpha:docs/atlas.md", expected_commit="different-commit")
        self.assertFalse(mismatch["found"])
        self.assertEqual(mismatch["errors"][0]["expected_commit"], "different-commit")

    def test_scope_routes_repository_and_memory_without_score_fusion(self):
        native = Mock()
        native.configured = True
        adapter = Mock()
        adapter.available = False
        adapter.native_memory = native
        adapter.memory_status.return_value = {"configured": True, "reachable": True, "status": "ready", "embedding": {"available": False, "strategy": "keyword-only"}}
        native_item = {
            "id": "memorycore:L0:message-1",
            "content": "native conversation evidence",
            "citation": {"source": "memorycore", "layer": "L0", "memory_id": "memorycore:L0:message-1", "evidence": "native conversation evidence"},
            "_native_memory": True,
        }
        adapter.memory_search.return_value = [native_item]
        with patch("core.discover_adapter", return_value=adapter), patch("core.Adapter", return_value=adapter):
            repository = core.search(self.alpha, "Atlas evidence", local=True, scope="repository")
            self.assertTrue(repository["diagnostics"]["adapters"][0]["memory_skipped"])
            adapter.memory_search.assert_not_called()
            memory = core.search(self.alpha, "conversation evidence", local=True, scope="memory")
            self.assertEqual(memory["verified"][0]["memory"]["layer"], "L0")
            adapter.memory_search.assert_called_once()
            combined = core.search(self.alpha, "conversation evidence", local=True, scope="all")
            self.assertIn("repository", combined["groups"])
            self.assertIn("memory", combined["groups"])
            self.assertEqual(combined["verified"], [])
            self.assertEqual(combined["groups"]["memory"]["verified"][0]["source"], "memorycore")

    def test_init_builds_non_git_knowledge_source_and_persists_local_index(self):
        knowledge = Path(self.temp.name) / "knowledge"
        knowledge.mkdir()
        (knowledge / "README.md").write_text("# Knowledge\n", encoding="utf-8")
        (knowledge / "facts.md").write_text("# Facts\nA portable knowledge base entry.\n", encoding="utf-8")
        result = core.init_source(str(knowledge), "portable", sync=True)
        self.assertTrue(result["initialized"])
        self.assertEqual(result["sync"]["sources"][0]["repository_index"], "local_structured")
        search = core.search(knowledge, "portable knowledge base", local=True, scope="repository")
        self.assertEqual(search["verified"][0]["source"], "portable")
        self.assertEqual(search["verified"][0]["commit_type"], "local_directory")
        self.assertTrue(Path(result["sync"]["sources"][0]["index"]["path"]).is_file())

        nested = Path(self.temp.name) / "nested-knowledge"
        (nested / "docs").mkdir(parents=True)
        (nested / "docs" / "fact.md").write_text("# Fact\nNested knowledge evidence.\n", encoding="utf-8")
        nested_result = core.init_source(str(nested), "nested", sync=True)
        self.assertTrue(nested_result["initialized"])
        nested_search = core.search(nested, "nested knowledge evidence", local=True, scope="repository")
        self.assertEqual(nested_search["verified"][0]["source"], "nested")

        secret = knowledge / ".env"
        secret.write_text("API_TOKEN=not-for-retrieval\n", encoding="utf-8")
        self.assertFalse(core.get_result(knowledge, "portable:.env")["found"])

    def test_one_command_installer_configures_skill_hosts_mcp_and_source(self):
        machine = Path(self.temp.name) / "other-machine"
        machine.mkdir()
        workspaces = [machine / "workspace-alpha", machine / "workspace-beta"]
        for workspace in workspaces:
            workspace.mkdir()
        openclaw_config = machine / ".openclaw" / "openclaw.json"
        openclaw_config.parent.mkdir()
        openclaw_config.write_text(json.dumps({
            "agents": {
                "list": [
                    {"id": "alpha", "workspace": str(workspaces[0]), "skills": ["rlvr-memory"], "tools": {"alsoAllow": []}},
                    {"id": "beta", "workspace": str(workspaces[1])},
                ]
            },
            "plugins": {"entries": {"rlvr-memory-autocapture": {"enabled": True, "config": {"guardEnabled": True}}}},
        }), encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "HOME": str(machine),
            "XDG_DATA_HOME": str(machine / "data"),
            "XDG_CONFIG_HOME": str(machine / "config"),
            "XDG_CACHE_HOME": str(machine / "cache"),
            "CODEX_HOME": str(machine / ".codex"),
            "CLAUDE_CONFIG_DIR": str(machine / ".claude"),
        })
        environment.pop("REPOSITORY_MEMORY_CONFIG", None)
        missing_selection = subprocess.run([
            sys.executable,
            str(SCRIPTS / "install.py"),
            "--target",
            "openclaw",
            "--no-mcp",
            "--no-verify",
            "--openclaw-config",
            str(openclaw_config),
            "--json",
        ], text=True, capture_output=True, env=environment)
        self.assertNotEqual(missing_selection.returncode, 0)
        self.assertIn("requires --openclaw-agent", missing_selection.stdout)
        installed = subprocess.run([
            sys.executable,
            str(SCRIPTS / "install.py"),
            "--all",
            "--no-mcp",
            "--openclaw-config",
            str(openclaw_config),
            "--source-root",
            str(self.alpha),
            "--source-local-only",
            "--openclaw-agent",
            "alpha",
            "--json",
        ], text=True, capture_output=True, check=True, env=environment)
        report = json.loads(installed.stdout)
        self.assertEqual(report["status"], "installed")
        self.assertEqual(report["targets"], ["claude", "codex", "openclaw"])
        self.assertTrue((machine / ".codex" / "skills" / "repository-memory" / "SKILL.md").is_file())
        self.assertTrue((machine / ".claude" / "skills" / "repository-memory" / "SKILL.md").is_file())
        self.assertTrue((workspaces[0] / "skills" / "repository-memory" / "SKILL.md").is_file())
        self.assertFalse((workspaces[1] / "skills" / "repository-memory").exists())
        configured = json.loads(openclaw_config.read_text(encoding="utf-8"))
        user_config = json.loads((machine / "config" / "repository-memory" / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(user_config["sources"][0]["local_only"])
        self.assertIn("repository-memory", configured["mcp"]["servers"])
        self.assertEqual(
            Path(configured["plugins"]["load"]["paths"][0]),
            openclaw_config.parent / "extensions" / "repository-memory-autocapture",
        )
        plugin = configured["plugins"]["entries"]["repository-memory-autocapture"]
        self.assertTrue(plugin["config"]["guardEnabled"])
        self.assertEqual(plugin["config"]["enforcement"], "audit")
        self.assertEqual(plugin["config"]["agentIds"], ["alpha"])
        self.assertTrue(plugin["hooks"]["allowConversationAccess"])
        self.assertFalse(configured["plugins"]["entries"]["rlvr-memory-autocapture"]["enabled"])
        alpha = next(agent for agent in configured["agents"]["list"] if agent["id"] == "alpha")
        beta = next(agent for agent in configured["agents"]["list"] if agent["id"] == "beta")
        self.assertIn("repository-memory", alpha["skills"])
        self.assertNotIn("rlvr-memory", alpha["skills"])
        self.assertIn("repository-memory__memory_search", alpha["tools"]["alsoAllow"])
        self.assertNotIn("repository-memory", beta.get("skills", []))
        self.assertNotIn("repository-memory__memory_search", beta.get("tools", {}).get("alsoAllow", []))
        wrapper = machine / ".local" / "bin" / "repository-memory"
        self.assertTrue(wrapper.is_file())
        searched = subprocess.run([
            str(wrapper),
            "search",
            "Atlas evidence",
            "--scope",
            "repository",
            "--local",
            "--json",
        ], text=True, capture_output=True, check=True, env=environment)
        payload = json.loads(searched.stdout)
        self.assertFalse(payload["abstain"])
        self.assertEqual(payload["verified"][0]["path"], "docs/atlas.md")

    def test_openclaw_allowlist_is_ready_without_covering_unselected_agents(self):
        home = Path(self.temp.name) / "openclaw-home"
        config_path = home / ".openclaw" / "openclaw.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({
            "agents": {"list": [{"id": "yaole"}, {"id": "other"}]},
            "mcp": {"servers": {"repository-memory": {}}},
            "plugins": {"entries": {
                "repository-memory-autocapture": {"enabled": True, "config": {
                    "enabled": True,
                    "guardEnabled": True,
                    "enforcement": "audit",
                    "agentIds": ["yaole"],
                }},
                "active-memory": {"enabled": False},
                "memmy-memory": {"enabled": False},
            }},
        }), encoding="utf-8")
        with patch.dict(os.environ, {"HOME": str(home)}):
            routing = core._openclaw_routing()
        self.assertEqual(routing["status"], "ready")
        self.assertEqual(routing["guard"], "advisory")
        self.assertEqual(routing["agents"]["scope"], "allowlist")
        self.assertEqual(routing["agents"]["covered"], ["yaole"])
        self.assertEqual(routing["agents"]["excluded"], ["other"])

    def test_mcp_init_is_explicit_and_ingest_accepts_session_payload(self):
        knowledge = Path(self.temp.name) / "mcp-knowledge"
        knowledge.mkdir()
        (knowledge / "README.md").write_text("# MCP\n", encoding="utf-8")
        initialized = core._mcp_dispatch("memory_init", {"path": str(knowledge), "source_id": "mcp"})
        self.assertTrue(initialized["initialized"])
        with patch("core.ingest_session", return_value={"ok": True, "memory": {"supported_layers": ["L0", "L1", "L2", "L3"]}}) as ingest:
            result = core._mcp_dispatch("memory_ingest", {"session": {"messages": [{"role": "user", "content": "remember this"}]}, "source": "mcp"})
            self.assertTrue(result["ok"])
            ingest.assert_called_once()

    def test_mcp_shared_team_memory_tools_use_same_runtime(self):
        published = core._mcp_dispatch("memory_publish", {"memory": {
            "type": "handoff",
            "title": "Review handoff",
            "content": "Review the isolated worktree decision before changing the runner.",
            "scope": {"repo": "alpha", "issue": "A-7"},
            "provenance": {"agent": "coder"},
        }, "status": "active"})
        memory_id = published["published"][0]["memory"]["id"]
        context = core._mcp_dispatch("memory_context", {"query": "isolated worktree runner", "repo": "alpha"})
        self.assertEqual(context["context"]["handoffs"][0]["id"], memory_id)
        feedback = core._mcp_dispatch("memory_feedback", {"id": memory_id, "rating": "helpful", "note": "used"})
        self.assertTrue(feedback["ok"])

    def test_memory_scope_works_without_repository_source(self):
        client = Mock()
        client.configured = True
        client.health.return_value = {"configured": True, "reachable": True, "status": "ready", "supported_layers": ["L0", "L1", "L2", "L3"], "embedding": {"available": False, "strategy": "keyword-only"}}
        adapter = Mock()
        adapter.native_memory = client
        adapter.memory_status.return_value = client.health.return_value
        adapter.memory_search.return_value = [{
            "id": "memorycore:L0:standalone-1",
            "content": "standalone conversation evidence",
            "citation": {"source": "memorycore", "layer": "L0", "memory_id": "standalone-1", "evidence": "standalone conversation evidence"},
            "_native_memory": True,
        }]
        with patch("core.discover_sources", side_effect=RuntimeError("no knowledge source configured")), patch("core.native_memory_client", return_value=client), patch("core.Adapter", return_value=adapter):
            result = core.search(None, "standalone conversation", scope="memory")
        self.assertFalse(result["abstain"])
        self.assertEqual(result["sources"][0]["id"], "memorycore")
        self.assertEqual(result["verified"][0]["memory"]["layer"], "L0")
        self.assertEqual(result["verified"][0]["citation"]["valid"], True)

    def test_doctor_reports_memory_readiness_without_repository_source(self):
        client = Mock()
        client.configured = True
        client.health.return_value = {"configured": True, "reachable": True, "status": "ready", "supported_layers": ["L0", "L1", "L2", "L3"], "embedding": {"available": False, "strategy": "keyword-only"}}
        with patch("core.configured_sources", return_value=[]), patch("core.resolve_root", side_effect=RuntimeError("no knowledge source configured")), patch("core.native_memory_client", return_value=client), patch("adapters.native_memory_client", return_value=client):
            report = core.doctor(None)
        self.assertTrue(report["ok"])
        self.assertEqual(report["repository"]["status"], "not_configured")
        self.assertEqual(report["memory"]["supported_layers"], ["L0", "L1", "L2", "L3"])

    def test_local_memory_fallback_is_durable_without_native_backend(self):
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        session = Path(self.temp.name) / "local-session.json"
        session.write_text(json.dumps({
            "session_id": "local-session",
            "messages": [{"role": "user", "content": "remember portable local memory"}],
        }), encoding="utf-8")
        ingested = core.ingest_session(None, str(session))
        self.assertTrue(ingested["ok"])
        self.assertEqual(ingested["source"], "local-memory")
        self.assertEqual(ingested["memory"]["supported_layers"], ["L0", "L1"])
        found = core.search(None, "portable local memory", scope="memory")
        self.assertFalse(found["abstain"])
        self.assertEqual(found["verified"][0]["source"], "local-memory")
        self.assertEqual(found["verified"][0]["memory"]["layer"], "L1")
        fetched = core.get_result(None, found["verified"][0]["id"])
        self.assertTrue(fetched["found"])
        self.assertEqual(fetched["source"], "local-memory")
        self.assertFalse(subprocess.check_output(["git", "-C", str(self.alpha), "status", "--porcelain"], text=True))

    def test_capture_turn_creates_one_team_candidate_without_accepting_it(self):
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        payload = {
            "session_id": "capture-session",
            "run_id": "capture-run",
            "agent_id": "coder",
            "messages": [
                {"role": "user", "content": "记录这次决定"},
                {"role": "assistant", "content": "决定：每个 issue 使用独立 worktree，避免共享 canonical clone。"},
            ],
        }
        first = core.capture_turn(None, payload)
        second = core.capture_turn(None, payload)
        self.assertTrue(first["ok"])
        self.assertEqual(first["team_memory"]["status"], "candidate")
        self.assertTrue(first["team_memory"]["created"])
        self.assertTrue(second["duplicate"])
        context = core.memory_context(None, "独立 worktree canonical clone")
        self.assertEqual(context["context"]["team_memory"], [])
        self.assertTrue(context["context"]["team_candidates"])

    def test_memory_ingest_does_not_require_repository_source(self):
        client = Mock()
        client.configured = True
        client.health.return_value = {"configured": True, "reachable": True, "status": "ready", "supported_layers": ["L0", "L1", "L2", "L3"]}
        adapter = Mock()
        adapter.native_memory = client
        adapter.memory_status.return_value = client.health.return_value
        adapter.ingest_session.return_value = {"verified": True, "pipeline": "test"}
        with patch("core.native_memory_client", return_value=client), patch("core.Adapter", return_value=adapter):
            result = core.ingest_session_payload(None, {"messages": [{"role": "user", "content": "remember standalone"}]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "memorycore")

    def test_native_ingest_verifies_l0_and_does_not_claim_l1_is_synchronous(self):
        config = MemoryCoreConfig(
            endpoint="http://127.0.0.1:8420",
            api_key=None,
            team_id="team",
            agent_id="agent",
            user_id="user",
        )
        client = MemoryCoreClient(config)
        session = Path(self.temp.name) / "native-session.json"
        session.write_text(json.dumps({
            "sessions": [{
                "sessionKey": "session-native",
                "messages": [{"role": "user", "content": "remember this durable fact"}],
            }],
        }), encoding="utf-8")
        responses = iter([
            {"code": 0, "data": {"accepted_ids": ["message-native"], "total_count": 1}},
            {"code": 0, "data": {"messages": [{"id": "message-native", "content": "remember this durable fact"}]}},
            {"code": 0, "data": {"status": "ok"}},
        ])
        with patch.object(client, "_request", side_effect=lambda *_args, **_kwargs: next(responses)):
            result = client.ingest(session)
        self.assertTrue(result["verified"])
        self.assertTrue(result["l0_verified"])
        self.assertEqual(result["l1_status"], "pending")
        self.assertFalse(result["canonical_repo_changed"])

    def test_fixed_revision_evaluation_uses_detached_snapshot(self):
        queries = Path(self.temp.name) / "revision-queries.jsonl"
        qrels = Path(self.temp.name) / "revision-qrels.jsonl"
        # Deliberately make the canonical source id differ from the temporary
        # repository directory name.  This is how real detached snapshots are
        # evaluated and catches accidental cache-name IDs in the evaluator.
        queries.write_text(json.dumps({"id": "q1", "query": "Atlas evidence", "intent": "exact", "source_scope": "canonical-alpha"}) + "\n", encoding="utf-8")
        qrels.write_text(json.dumps({"query_id": "q1", "document_id": "canonical-alpha:docs/atlas.md", "source": "canonical-alpha", "path": "docs/atlas.md", "relevance": 2}) + "\n", encoding="utf-8")
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True).strip()
        os.environ["REPOSITORY_MEMORY_DISABLE_ADAPTER"] = "1"
        report = evaluate_queries(self.alpha, queries, qrels, local=True, revision=commit)
        self.assertEqual(report["evaluated_commit"], commit)
        self.assertEqual(report["scope"], "repository")
        self.assertEqual(report["precision_at_1"], 1.0)
        self.assertTrue(report["queries_sha256"])
        self.assertTrue(report["qrels_sha256"])
        all_report = evaluate_queries(self.alpha, queries, qrels, local=True, revision=commit, scope="all")
        self.assertEqual(all_report["precision_at_1"], 1.0)
        self.assertEqual(all_report["rows"][0]["selected_scope"], "repository")


if __name__ == "__main__":
    unittest.main()
