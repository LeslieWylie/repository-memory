#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import sqlite3
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
from fallback import _claim_support, _compound_parts, _fts_candidates, carved_query_terms, paths, query_terms
from local_index import _ensure_fts, _ensure_path_fts
from memorycore import MemoryCoreClient, MemoryCoreConfig, MemoryCoreError
from memmy import MemmyClient, MemmyConfig
from mcp_server import SERVER_VERSION
from snapshot import _snapshot_lock, prepare_view, snapshot_lock_backend
from team_memory import TeamMemoryStore
from tokenize_query import as_iso_date, date_aliases
from version import VERSION
from benchmark import _semantic_override
from memos_lifecycle import backpropagate, classify_turn, ready_buckets

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

    def test_large_index_fts_keeps_filename_cjk_anchor(self):
        destination = Path(self.temp.name) / "derived-index.json"
        documents = [{"path": "standup/武垚乐.md", "text": "日报：今天完成了实验。"}]
        fts = _ensure_fts(destination, documents)
        path_fts = _ensure_path_fts(destination, documents)
        self.assertIn("standup/武垚乐.md", _fts_candidates({"fts_path": str(fts), "fts_path_paths": str(path_fts)}, ["武垚乐"]))

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

    def test_semantic_benchmark_override_is_ephemeral(self):
        os.environ.pop("REPOSITORY_MEMORY_SEMANTIC_MODEL", None)
        with _semantic_override("Alibaba-NLP/gte-multilingual-base"):
            self.assertEqual(os.environ["REPOSITORY_MEMORY_SEMANTIC_MODEL"], "Alibaba-NLP/gte-multilingual-base")
            self.assertEqual(os.environ["REPOSITORY_MEMORY_SEMANTIC_ENABLED"], "1")
        self.assertNotIn("REPOSITORY_MEMORY_SEMANTIC_MODEL", os.environ)
        self.assertNotIn("REPOSITORY_MEMORY_SEMANTIC_ENABLED", os.environ)

    def test_index_reports_scale_metadata(self):
        from local_index import build
        view = prepare_view(SourceSpec(id="alpha", root=self.alpha, repository="alpha"), local=True)
        value = build(view)
        self.assertEqual(value["document_count"], len(value["documents"]))
        self.assertGreaterEqual(value["text_bytes"], 0)
        self.assertGreater(value["index_bytes"], 0)
        self.assertEqual(VERSION, "0.7.18")

    def test_multisource_search_has_verified_and_candidates(self):
        result = core.search(None, "Atlas evidence", limit=5)
        self.assertFalse(result["abstain"])
        self.assertEqual({item["source"] for item in result["verified"]}, {"alpha", "beta"})
        self.assertEqual(result["verified"][0]["citation"]["valid"], True)
        self.assertEqual(result["results"], result["answerable"])
        self.assertEqual(len(result["answerable"]), len(result["verified"]))

        pending = core.search(None, "pending candidate", limit=5)
        self.assertTrue(pending["abstain"])
        self.assertEqual(len(pending["verified"]), 0)
        self.assertEqual(pending["candidates"][0]["evidence_status"], "pending")

    def test_a_recalled_question_is_never_its_own_answer(self):
        """Conversation capture stores the question; retrieval must not cite it.

        Found live: asking a question twice retrieved the first asking as an
        accepted L1 memory.  Its excerpt matched every query term by
        construction, so ``claim_support`` was ``direct`` and it was reported
        as answerable — the system citing the question as its own evidence.
        """

        query = "octo-daemon 升级到哪个版本了?当时是怎么验证的?"
        echo = {"excerpt": query, "support": {"claim_support": "direct"}}
        punctuated = {"excerpt": "octo-daemon 升级到哪个版本了？当时是怎么验证的？ ", "support": {"claim_support": "direct"}}
        fragment = {"excerpt": "octo-daemon 升级到哪个版本了", "support": {"claim_support": "direct"}}
        real = {
            "excerpt": f"{query} 答:升级到 0.5.0，commit fcec9177，三项验收全绿，回退路径已备好未用。",
            "support": {"claim_support": "direct"},
        }

        answerable = core._answerable_items([echo, punctuated, fragment, real], query)
        self.assertEqual(answerable, [real], "only the excerpt that adds an answer may be answerable")

        # Without a query the filter must not fire — the claim-support gate is
        # still the contract for every existing caller.
        self.assertEqual(len(core._answerable_items([echo, real])), 2)

        # An unrelated excerpt is untouched by echo detection.
        unrelated = {"excerpt": "回退路径都备好未用", "support": {"claim_support": "direct"}}
        self.assertIn(unrelated, core._answerable_items([unrelated], query))

    def test_query_echoes_do_not_consume_the_result_budget(self):
        """Echoes must be dropped before the limit slice, not after.

        Filtering them only out of ``answerable`` left them holding result
        slots.  Measured live: a recall for
        ``octo-daemon 升级到哪个版本了?当时是怎么验证的?`` returned five verified
        memory hits that were all that same question, captured verbatim from
        earlier turns; the record that answers it never made the cut.  The
        drop is reported in diagnostics so a plane crowded out by its own echo
        is distinguishable from one that genuinely holds nothing.
        """

        query = "octo-daemon 升级到哪个版本了?当时是怎么验证的?"
        limit = 2
        echoes = [{"excerpt": query, "support": {"claim_support": "direct"}} for _ in range(3)]
        real = {
            "excerpt": f"{query} 答:升级到 0.5.0，commit fcec9177，三项验收全绿。",
            "support": {"claim_support": "direct"},
        }
        group = {"verified": [*echoes, real], "candidates": []}

        # Slicing first is what shipped, and it loses the answer entirely.
        self.assertNotIn(real, group["verified"][:limit])

        kept = [item for item in group["verified"] if not core._is_query_echo(item, query)]
        self.assertEqual(kept[:limit], [real])
        self.assertEqual(len(group["verified"]) - len(kept), 3)

    def test_captured_questions_are_echoes_regardless_of_the_current_query(self):
        """The echo test keys on what the record is, not on string overlap.

        The lexical version compared each excerpt against the *current* query,
        so it only ever caught a verbatim re-ask.  Measured live: searching
        ``octo-daemon 升级`` returned four "answerable" memory hits whose
        excerpts were all the longer question
        ``octo-daemon 升级到哪个版本了？当时是怎么验证的？``.  None resembled the
        short query closely enough to trip a similarity test, so every one of
        them survived and the answer was crowded out — ``query_echo_dropped``
        read 0 while the plane was pure echo.

        A captured user turn ending in a question mark is a question whatever
        was typed this time, so it is never evidence.
        """

        query = "octo-daemon 升级"
        stored_question = {
            "excerpt": "octo-daemon 升级到哪个版本了？当时是怎么验证的？",
            "memory": {"layer": "L1", "role": "user"},
            "support": {"claim_support": "direct"},
        }
        # The lexical arm alone cannot see this: the excerpt is far longer than
        # 1.15x the query and does not contain it as a suffix-free substring.
        self.assertFalse(core._normalize_echo(stored_question["excerpt"]) in core._normalize_echo(query))
        self.assertTrue(core._is_query_echo(stored_question, query))
        # ...and with no query at all, which is what the repository plane passes.
        self.assertTrue(core._is_query_echo(stored_question, ""))

        # A user turn that states a fact is evidence, not an echo.
        user_fact = {
            "excerpt": "我们把 octo-daemon 升级到了 0.5.0，commit fcec9177。",
            "memory": {"layer": "L1", "role": "user"},
            "support": {"claim_support": "direct"},
        }
        self.assertFalse(core._is_query_echo(user_fact, query))

        # An assistant answer is evidence even when it restates the question.
        assistant_answer = {
            "excerpt": "octo-daemon 升级到哪个版本了？升级到 0.5.0。",
            "memory": {"layer": "L1", "role": "assistant"},
            "support": {"claim_support": "direct"},
        }
        self.assertFalse(core._is_query_echo(assistant_answer, query))

        self.assertEqual(
            core._answerable_items([stored_question, user_fact, assistant_answer], query),
            [user_fact, assistant_answer],
        )

    def test_a_prior_answer_is_answerable_even_though_it_omits_question_words(self):
        """``direct`` is unreachable for a question, so it cannot be the bar.

        ``_claim_support`` asks whether the excerpt contains every query term.
        An answer never repeats "哪个" or "了", so against a question the only
        text that can score ``direct`` is that same question.  Measured live:
        the assistant turns holding "octo-daemon 从 0.1.0 升级到 0.5.0, commit
        fcec9177" scored ``partial`` at ``coverage 0.33`` and were withheld,
        while the captured question scored ``direct`` and was served.

        A repository citation keeps the ``direct`` bar — a document quote
        covering half a compound question is how confident wrong answers get
        made.  A prior assistant turn is answerable once retrieval matched
        anything in it, and carries its coverage so nothing is hidden.
        """

        answer = {
            "excerpt": "octo-daemon 从 0.1.0 升级到 0.5.0，commit fcec9177。",
            "memory": {"layer": "L1", "role": "assistant"},
            "support": {"claim_support": "partial", "coverage": 0.3333, "unmatched_terms": ["升级到哪个版本了"]},
        }
        unrelated = {
            "excerpt": "群规：武垚乐发消息时所有bot必须回复。",
            "memory": {"layer": "L1", "role": "assistant"},
            "support": {"claim_support": "unknown", "coverage": 0.0},
        }
        partial_document = {
            "excerpt": "octo-daemon 的部署说明。",
            "support": {"claim_support": "partial", "coverage": 0.3333},
        }

        query = "octo-daemon 升级到哪个版本了？当时是怎么验证的？"
        self.assertEqual(core._answerable_items([answer, unrelated, partial_document], query), [answer])
        # The support block travels with the item; the caller can still see how
        # much of the compound question that turn actually covered.
        self.assertEqual(answer["support"]["coverage"], 0.3333)

    def test_memory_plane_overfetches_so_echoes_cannot_empty_it(self):
        """Filtering after a ``limit``-sized fetch can only ever empty the plane.

        Captured questions outrank the answers that quote them — a short query
        matches a stored question almost exactly, while the assistant turn
        answering it is long and dilutes the same terms.  Measured live:
        ``octo-daemon 升级`` returned six store hits, four of them that same
        question asked on four earlier days, and the three assistant turns
        holding ``0.5.0, commit fcec9177`` never entered the window.  Dropping
        the four left nothing.  The fetch has to be wider than the slice.
        """

        requested: list[int] = []

        class _Recorder:
            available = True
            name = "standalone-memory"
            protocol = "local"

            def memory_status(self):
                return {"reachable": True, "backend": "standalone-memory", "embedding": {"available": True}}

            def memory_search(self, query, limit):
                requested.append(limit)
                return []

        view = Mock()
        view.spec.id = "standalone-memory"
        _items, _diagnostic = core._memory_items(view, _Recorder(), "octo-daemon 升级", 5)
        self.assertTrue(requested)
        self.assertGreater(requested[0], 5)

    def test_memory_search_results_carry_the_record_role(self):
        """``role`` has to survive the search path for the echo test to work.

        The store has always had a ``role`` column and ``get()`` returned it,
        but the search formatter dropped it, so the answer surface could only
        guess a captured question from its text.
        """

        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        from standalone_memory import standalone_memory_client

        client = standalone_memory_client()
        connection = client._connect()
        try:
            client._upsert(
                connection,
                record_id="local:L1:role-question",
                layer="L1",
                session_id="role-episode",
                message_index=0,
                role="user",
                content="octo-daemon 升级到哪个版本了？",
                metadata={},
            )
            client._upsert(
                connection,
                record_id="local:L1:role-statement",
                layer="L1",
                session_id="role-episode",
                message_index=2,
                role="user",
                content="octo-daemon 升级这件事我们上周就排期了。",
                metadata={},
            )
            client._upsert(
                connection,
                record_id="local:L1:role-answer",
                layer="L1",
                session_id="role-episode",
                message_index=1,
                role="assistant",
                content="octo-daemon 升级到 0.5.0，commit fcec9177。",
                metadata={},
            )
            connection.commit()
        finally:
            connection.close()

        roles = {item["id"]: item.get("memory_role") for item in client.search("octo-daemon 升级", limit=5)}
        self.assertEqual(roles.get("local:L1:role-answer"), "assistant")
        # A user turn that states a fact is evidence and keeps its role.
        self.assertEqual(roles.get("local:L1:role-statement"), "user")
        # The interrogative one never enters the ranking, so it cannot consume
        # a slot the answer needs — see ``is_question_turn``.
        self.assertNotIn("local:L1:role-question", roles)

    def test_a_shallow_search_reaches_the_answer_past_echoes_and_candidates(self):
        """Recall fetches five, so rank six is the same as not stored.

        Measured on the live store before this was fixed: eleven verbatim
        copies of "octo-daemon 升级到哪个版本了？当时是怎么验证的？" scored 1.65
        and held ranks 1-11, ten unreviewed L2 candidate envelopes held the
        next ten, and the assistant turns holding the answer sat at rank 24.
        The recall hook asks for five and got nothing usable, every time.

        Both crowders are discarded by the answer surface, so they were
        occupying slots to be thrown away.  This pins the depth, not the
        ordering: at the depth recall actually uses, the answer must be there.
        """

        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        from standalone_memory import standalone_memory_client

        client = standalone_memory_client()
        connection = client._connect()
        try:
            for index in range(11):
                client._upsert(
                    connection,
                    record_id=f"local:L1:echo-{index}",
                    layer="L1",
                    session_id="crowding-episode",
                    message_index=index,
                    role="user",
                    content="octo-daemon 升级到哪个版本了？当时是怎么验证的？",
                    metadata={},
                )
            for index in range(10):
                client._upsert(
                    connection,
                    record_id=f"local:L2:candidate-{index}",
                    layer="L2",
                    session_id="crowding-episode",
                    message_index=100 + index,
                    role="system",
                    content=(
                        "status: candidate\nlayer: L2\ngenerated: true\n"
                        f"octo-daemon 升级到哪个版本了 当时是怎么验证的 摘要 {index}"
                    ),
                    metadata={},
                    status="candidate",
                    generated=True,
                    accepted=False,
                )
            client._upsert(
                connection,
                record_id="local:L1:crowding-answer",
                layer="L1",
                session_id="crowding-episode",
                message_index=200,
                role="assistant",
                content="octo-daemon 从 0.1.0 升级到 0.5.0，构建 commit 是 fcec9177。",
                metadata={},
            )
            connection.commit()
        finally:
            connection.close()

        ids = [item["id"] for item in client.search("octo-daemon 升级到哪个版本了？当时是怎么验证的？", limit=5)]
        self.assertIn("local:L1:crowding-answer", ids)
        self.assertFalse([value for value in ids if value.startswith("local:L1:echo-")])

    def test_auto_scope_names_the_planes_that_answered(self):
        """``abstain`` is repository-only, so it cannot be the whole signal.

        Live replay found the trap: a question whose answer lived in
        conversation memory came back ``abstain: true`` with
        ``groups.memory.answerable`` populated.  ``abstain`` must stay
        repository-only — letting uncited memory suppress an abstention would
        defeat the evidence guards — so ``answered_by`` carries the rest.
        """

        auto = core.search(None, "Atlas evidence", limit=5)
        self.assertIsInstance(auto["answered_by"], list)
        self.assertEqual(
            "repository" in auto["answered_by"],
            bool(auto["answerable"]),
            "the repository plane is listed exactly when the top-level surface answers",
        )
        for plane in auto["answered_by"]:
            self.assertIn(plane, {"repository", "memory", "team"})
            group = auto["groups"][plane]
            self.assertTrue(group.get("answerable") or group.get("active"))

        # MCP clients may retain only a bounded prefix of a large structured
        # result.  The grouped answer planes must therefore serialize before
        # verbose repository candidates, or a valid memory answer can be
        # truncated out of the agent-visible tool result.
        serialized = json.dumps(auto, ensure_ascii=False)
        self.assertLess(serialized.index('"groups"'), serialized.index('"verified"'))

        # A caller must be able to test the key, not the build.
        negative = core.search(None, "fictional benchmark ZZZQWE")
        self.assertEqual(negative["answered_by"], [])
        self.assertTrue(negative["abstain"])

        # Explicit scopes keep the old shape; the key is additive.
        self.assertIsNone(core.search(None, "Atlas evidence", scope="repository")["answered_by"])

    def test_team_plane_answers_only_with_claim_support(self):
        """A match is not an answer: one shared generic word must not put
        ``team`` into ``answered_by``.

        Measured live: "我们公司什么时候上市" came back answered_by=['team']
        carrying a sync-timeout record that shared one generic term — a host
        following the Skill ("answer from the named group") would have
        fabricated an IPO answer out of an ops note.
        """

        from team_memory import team_memory_store

        store = team_memory_store()
        record = store.publish({
            "type": "discovery",
            "title": "网关限流阈值定为每分钟六百次",
            "content": "网关限流阈值定为每分钟六百次，超过就排队，压测通过。",
            "provenance": {"agent_id": "yaole", "citations": ["README.md"]},
            "confidence": 0.9,
        })["memory"]
        store.activate(record["id"], reviewer="reviewer")

        supported = core.search(None, "网关限流阈值是多少")
        self.assertIn("team", supported["answered_by"])
        top = supported["groups"]["team"]["active"][0]
        self.assertEqual(top["support"]["claim_support"], "direct")

        # Shares only "阈值" with the record; the claim itself is fabricated.
        leak = core.search(None, "上市估值的阈值是多少")
        self.assertNotIn("team", leak["answered_by"])
        self.assertTrue(leak["groups"]["team"]["abstain"])
        # The lead stays visible — gating answerability must not hide it.
        if leak["groups"]["team"]["active"]:
            self.assertIn("support", leak["groups"]["team"]["active"][0])

    def test_colloquial_interrogatives_and_spoken_dates_are_scaffolding(self):
        # Measured on live human phrasing: 在干嘛 / 做得怎么样 blocked answerable
        # questions, and 8月20号 is the spoken register of 8月20日.  The exact
        # term list is tokenizer-specific (jieba words vs builtin n-grams), so
        # assert the contract — the name survives, the colloquial scaffolding
        # never becomes a jieba-path claim — not one path's output verbatim.
        from tokenize_query import tokenizer_status
        colloquial = query_terms("武垚乐昨天在干嘛")
        self.assertIn("武垚乐", colloquial)
        self.assertNotIn("昨天", colloquial)
        if tokenizer_status().get("name") == "jieba":
            self.assertEqual(colloquial, ["武垚乐"])
            self.assertNotIn("得怎么样", query_terms("GLM 迁移做得怎么样了"))
        self.assertEqual(query_terms("武垚乐 8月18号 做了什么"), query_terms("武垚乐 8月18日 做了什么"))
        self.assertIn("2026-08-20", query_terms("2026年8月20号 谁提交了"))
        # Modal prefixes and directional complements are closed glue classes:
        # jieba lexicalizes 要切/接进来 as words, the corpus writes 切/接入, and
        # requiring the glue held answerable questions at partial forever.
        # 要切 only exists as a term on the jieba path — the builtin marker
        # split already severs it at the modal — so that assertion is scoped.
        self.assertIn("接进来", carved_query_terms("kimi 的日志接进来了吗"))
        if tokenizer_status().get("name") == "jieba":
            self.assertIn("要切", carved_query_terms("为什么要切 cuDNN"))
        # A join never gates the claim when the segmenter gave real words: the
        # requirement keeps the components instead.
        support = _claim_support(
            ["27b", "模型上线", "模型", "上线"],
            "Qwen 27B 模型优化上线,BF16 全精度",
            1, 1,
            real_terms=frozenset({"27b", "模型", "上线"}),
            carved=frozenset({"模型上线"}),
        )
        self.assertEqual(support["claim_support"], "direct")
        self.assertNotIn("模型上线", support["unmatched_terms"])

    def test_auto_scope_recalls_every_plane_without_changing_the_answer_surface(self):
        """One call must reach all three planes and still answer like before.

        ``auto`` is the default so an agent never has to pick a plane before it
        is allowed to ask.  The top-level surface stays repository-only: Git
        citations remain the mainline answer, and an existing caller reading
        ``verified``/``results`` sees exactly what ``scope="repository"``
        returns.
        """

        auto = core.search(None, "Atlas evidence", limit=5)
        repository = core.search(None, "Atlas evidence", limit=5, scope="repository")

        self.assertEqual(auto["scope"], "auto")
        self.assertEqual(auto["verified"], repository["verified"])
        self.assertEqual(auto["results"], repository["results"])
        self.assertEqual(auto["answerable"], repository["answerable"])
        self.assertEqual(auto["abstain"], repository["abstain"])
        # ``auto`` must not borrow the memory lane's stronger strategy to
        # describe a repository answer.
        self.assertEqual(auto["retrieval_mode"], repository["retrieval_mode"])

        self.assertEqual(set(auto["groups"]), {"repository", "memory", "team"})
        self.assertIsNone(repository["groups"])

        # Team records are experience provenance, not Git citations, so they
        # never appear under ``verified``.
        team = auto["groups"]["team"]
        self.assertEqual(set(team), {"active", "candidates", "abstain", "retrieval_mode"})
        self.assertNotIn("verified", team)
        self.assertLessEqual(len(team["active"]), 5)
        self.assertLessEqual(len(team["candidates"]), 5)

        self.assertIn("planes", auto["diagnostics"])

    def test_auto_scope_still_abstains_on_a_negative_query(self):
        """Reaching more planes must not create an answer out of nothing."""

        negative = core.search(None, "fictional benchmark ZZZQWE")
        self.assertTrue(negative["abstain"])
        self.assertEqual(negative["verified"], [])
        self.assertEqual(set(negative["groups"]), {"repository", "memory", "team"})
        self.assertEqual(negative["groups"]["team"]["active"], [])

    def test_explicit_scopes_are_unchanged_by_the_auto_default(self):
        for scope in ("repository", "memory", "all"):
            with self.subTest(scope=scope):
                result = core.search(None, "Atlas evidence", limit=5, scope=scope)
                self.assertEqual(result["scope"], scope)
                self.assertNotIn("team", result.get("groups") or {})
        with self.assertRaises(ValueError):
            core.search(None, "Atlas evidence", scope="nonsense")

    def test_multisource_search_routes_explicit_anchor_to_matching_source(self):
        result = core.search(None, "alpha", limit=5)
        self.assertEqual(result["verified"][0]["source"], "alpha")

    def test_configured_default_source_prevents_silent_cross_repository_mix(self):
        self.write_config({
            "default_source": "beta",
            "sources": [
                {"id": "alpha", "root": str(self.alpha), "adapter": str(self.adapter)},
                {"id": "beta", "root": str(self.beta), "adapter": str(self.adapter)},
            ],
        })
        result = core.search(None, "Atlas evidence", limit=5)
        self.assertEqual({item["source"] for item in result["verified"]}, {"beta"})
        explicit = core.search(None, "Atlas evidence", source_id="alpha", limit=5)
        self.assertEqual({item["source"] for item in explicit["verified"]}, {"alpha"})

    def test_negative_and_local_structured_backend_are_conservative(self):
        negative = core.search(None, "fictional benchmark ZZZQWE")
        self.assertTrue(negative["abstain"])
        self.assertEqual(negative["retrieval_mode"], "abstain")
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
        # An uncommitted file elsewhere in the tree costs the citation its
        # commit pin, not its truth: the excerpt is still read back from disk
        # and matched.  The answer survives and says it is unpinned.
        self.assertFalse(dirty_result["abstain"])
        self.assertTrue(dirty_result["answerable"])
        self.assertEqual(dirty_result["verified"][0]["evidence_status"], "worktree")
        self.assertFalse(dirty_result["verified"][0]["citation"]["pinned"])
        self.assertTrue(dirty_result["verified"][0]["citation"]["valid"])
        self.assertFalse(dirty_result["verified"][0]["citation"]["stale"])
        # get() re-reads the file rather than reusing the search result, so it
        # has to reach the same verdict independently or a hit that answered
        # would fail the moment the caller asked to see it.
        fetched = core.get_result(None, dirty_result["verified"][0]["id"])
        self.assertTrue(fetched["found"])
        self.assertEqual(fetched["status"], "worktree")
        self.assertTrue(fetched["readback"]["verified"])
        self.assertTrue(fetched["result"]["citation"]["valid"])
        self.assertFalse(fetched["result"]["citation"]["pinned"])
        # Secret hygiene is independent of any of that and stays absolute.
        for bucket in ("verified", "candidates", "results", "answerable"):
            self.assertTrue(all(".env" not in (item.get("path") or "") for item in dirty_result[bucket]))

    def test_builtin_repository_projection_is_active_by_default(self):
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})

        result = core.search(None, "Atlas evidence", local=True)

        self.assertEqual(result["retrieval_mode"], "local-hybrid")
        self.assertTrue(result["diagnostics"]["semantic_available"])
        self.assertEqual(result["diagnostics"]["adapters"][0]["semantic"]["provider"], "builtin")
        self.assertEqual(result["diagnostics"]["adapters"][0]["semantic"]["strategy"], "local-hybrid")
        self.assertFalse(result["diagnostics"]["adapters"][0]["semantic"]["native_neural_model"])

    def test_normal_sync_never_grants_model_download_permission(self):
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})
        captured = {}

        def fake_semantic(view, local_index, deep=False, *, allow_download=False):
            captured["allow_download"] = allow_download
            return {"configured": True, "available": True, "provider": "builtin", "model": "builtin-char-ngram-v1", "dimension": 384, "indexed": True, "strategy": "local-hybrid"}

        with patch("core.ensure_semantic_index", side_effect=fake_semantic):
            synced = core.sync_index(None, local=True)

        self.assertFalse(captured["allow_download"])
        self.assertTrue(synced["sources"][0]["synced"])

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
        (standup / "李小明.md").write_text(
            "# 李小明\n\n## 2026-08-07\n\n推进 repository-memory MCP 接入和日志分析流水线。\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.alpha), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "standup"], check=True)
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})

        result = core.search(None, "李小明最近在干啥", local=True)

        self.assertFalse(result["abstain"])
        self.assertEqual(result["mode"], "temporal")
        self.assertEqual(result["verified"][0]["path"], "standup/李小明.md")
        self.assertEqual(result["verified"][0]["citation"]["valid"], True)
        self.assertIn("李小明", result["diagnostics"]["query_terms"])
        self.assertFalse(result["freshness"]["alpha"]["dirty"])

    def test_cjk_temporal_scaffolding_does_not_become_query_terms(self):
        terms = query_terms("最近的模型评审")
        self.assertIn("模型评审", terms)
        self.assertIn("模型", terms)
        self.assertIn("评审", terms)
        self.assertNotIn("最近的", terms)
        self.assertNotIn("的模型", terms)

    def test_carved_terms_are_separated_from_what_the_user_delimited(self):
        # A whitespace-bounded token is something the user typed and meant; a
        # fragment cut out of an unsegmented CJK run is this tokenizer's guess.
        query = "octo-daemon 的健康监控 cron 是怎么配置的？"
        carved = carved_query_terms(query)
        self.assertNotIn("octo-daemon", carved)
        self.assertNotIn("cron", carved)
        # Whatever the tokenizer cut out of the CJK runs is marked as its own.
        self.assertTrue(carved)
        self.assertFalse({"octo-daemon", "cron"} & carved)
        # An all-CJK token the markers left intact is the user's own token.
        self.assertNotIn("火山云", carved_query_terms("octo-loop 火山云"))

    def test_scaffolding_never_fuses_with_the_content_word_beside_it(self):
        # "是怎么配置" spans the seam between a question's scaffolding and its
        # subject.  It occurs in no document, so manufacturing it held coverage
        # below 1.0 forever — abstention by tokenizer rather than by evidence.
        # Neither tokenizer may produce it: jieba cuts 是/怎么/配置 apart, and
        # the builtin path cuts at the same scaffolding rather than splicing.
        terms = query_terms("octo-daemon 的健康监控 cron 是怎么配置的？")
        self.assertNotIn("是怎么配置", terms)
        self.assertFalse([term for term in terms if term.startswith(("是", "怎么"))])
        self.assertIn("配置", terms)
        self.assertIn("octo-daemon", terms)

        excerpt = "octo-daemon 的健康监控 cron 配置在每小时跑一次。"
        support = _claim_support(terms, excerpt, 1, 1)
        self.assertEqual(support["claim_support"], "direct")

    def test_ascii_identifiers_survive_segmentation_intact(self):
        # jieba cuts "rlvr-auto-survey" into five tokens and "octo-loop" into
        # three.  The outer word regex is the only reason they survive as
        # typed, so segmentation must run on CJK runs only, never on the whole
        # query.  This is the regression that guards that boundary.
        terms = query_terms("rlvr-auto-survey 的 octo-loop 调度链")
        self.assertIn("rlvr-auto-survey", terms)
        self.assertIn("octo-loop", terms)
        self.assertNotIn("rlvr", terms)
        self.assertNotIn("auto", terms)
        self.assertNotIn("loop", terms)
        # A mixed CJK/ASCII token the user delimited stays whole too, unless it
        # is a date — see test_a_chinese_date_normalizes_to_the_form_the_corpus_writes.
        self.assertIn("v2版本", query_terms("v2版本 的调度链"))

    def test_a_chinese_date_normalizes_to_the_form_the_corpus_writes(self):
        # People ask "8月18日"; Markdown headings say "## 2026-08-18".  The term
        # was required, occurred in no document, and held claim coverage below
        # 1.0 forever.  Normalizing is the same kind of operation as casefolding
        # — one date, two renderings — not a synonym table.
        terms = query_terms("武垚乐 8月18日 做了什么")
        self.assertIn("08-18", terms)
        self.assertIn("武垚乐", terms)
        # The raw form must be *replaced*, not joined: _claim_support requires
        # every term, so emitting both would abstain exactly as before.
        self.assertNotIn("8月18日", terms)
        # A year the user supplied is kept; a year they did not is not invented,
        # because guessing it answers a different question whenever the corpus
        # spans more than one year.
        self.assertIn("2026-08-18", query_terms("2026年8月18日 的调度链验证"))
        self.assertEqual(as_iso_date("8月18"), "08-18")
        # Not every digit-月-digit string is a date.  An impossible one falls
        # through to the ordinary token path rather than becoming "13--45".
        self.assertIsNone(as_iso_date("13月45日"))
        self.assertIsNone(as_iso_date("8月"))
        self.assertIn("13月45日", query_terms("13月45日 无效"))

    def test_evidence_written_in_chinese_proves_a_normalized_date(self):
        # The mirror direction: a document that writes the date in prose still
        # supports a query that normalized to ISO.  Only proof is symmetric —
        # ranking reads the index, which stores the corpus text as written.
        self.assertEqual(date_aliases("会议定在 8月18日 上午"), ["08-18"])
        support = _claim_support(
            ["08-18", "调度链"],
            "8月18日 完成调度链验证",
            1,
            1,
        )
        self.assertEqual(support["claim_support"], "direct")

    def test_a_date_is_not_split_into_bare_digits(self):
        # "08-18" split on the hyphen yields "08" and "18", which match every MR
        # number, GPU size and line count in the corpus.  That buried the one
        # line carrying the date and made the window picker cite a section
        # eleven months away from the question.  The hyphen split exists for
        # "long-context", which decomposes into concepts; digits do not.
        self.assertEqual(_compound_parts("long-context"), ["long", "context"])
        self.assertEqual(_compound_parts("08-18"), [])
        self.assertEqual(_compound_parts("2026-08-18"), [])
        # A part that is not purely numeric still decomposes.
        self.assertEqual(_compound_parts("llama-3.1"), ["llama", "3.1"])

    def test_a_compound_the_segmenter_splits_is_rejoined_from_neighbours(self):
        # jieba has no entry for 火山云 and returns 火山 + 云.  Joining adjacent
        # segments recovers the compound without a user dictionary or any list
        # of project names — the same reason there is no entity table anywhere
        # in retrieval.
        terms = query_terms("octo-loop 火山云")
        self.assertIn("火山云", terms)
        self.assertIn("octo-loop", terms)

    def test_aspect_particles_do_not_survive_as_scaffolding_residue(self):
        # "做了什么" used to leave "做了" behind: the marker list could only cut
        # literal strings out of an unsegmented run, so a verb and its aspect
        # particle stayed fused.  A segmenter separates them by construction.
        terms = query_terms("武垚乐 8月18日 做了什么")
        self.assertIn("武垚乐", terms)
        self.assertNotIn("做了", terms)
        self.assertNotIn("什么", terms)

    def test_tokenizer_reports_itself_and_degrades_without_jieba(self):
        import tokenize_query

        live = tokenize_query.tokenizer_status()
        self.assertIn(live["name"], {"jieba", "builtin-ngram"})
        self.assertTrue(live["available"])

        saved = (tokenize_query._JIEBA, tokenize_query._JIEBA_PROBED, tokenize_query._JIEBA_ERROR)
        try:
            tokenize_query._JIEBA = None
            tokenize_query._JIEBA_PROBED = True
            tokenize_query._JIEBA_ERROR = "ModuleNotFoundError: No module named 'jieba'"
            status = tokenize_query.tokenizer_status()
            self.assertEqual(status["name"], "builtin-ngram")
            self.assertTrue(status["available"])
            self.assertFalse(status["segments_cjk"])
            # Retrieval must still work, and the carved/unreachable machinery
            # that the n-gram path depends on must still be live: this is the
            # branch nobody runs in production, so the suite has to run it.
            terms = tokenize_query.query_terms("octo-daemon 的健康监控")
            self.assertIn("octo-daemon", terms)
            self.assertIn("健康监控", terms)
            self.assertIn("康监", terms)
            self.assertIn("康监", tokenize_query.carved_query_terms("octo-daemon 的健康监控"))
            self.assertNotIn("octo-daemon", tokenize_query.carved_query_terms("octo-daemon 的健康监控"))
        finally:
            tokenize_query._JIEBA, tokenize_query._JIEBA_PROBED, tokenize_query._JIEBA_ERROR = saved

    def test_every_retrieval_plane_tokenizes_a_chinese_question_the_same_way(self):
        # team_memory and local_memory did no CJK segmentation at all, so two of
        # the three planes that ``scope=auto`` fills were unreachable in
        # Chinese: the door was open and the rooms behind it were dark.
        import local_memory
        import standalone_memory
        import team_memory

        question = "李宁最近在做什么"
        for module_terms in (team_memory._terms, local_memory.LocalMemoryStore._terms, standalone_memory._terms):
            terms = module_terms(question)
            self.assertIn("李宁", terms, module_terms)
            self.assertNotIn("李宁最近在做什么", terms, module_terms)

    def test_team_memory_finds_a_chinese_record_despite_a_cjk_blind_index(self):
        # SQLite's stock FTS5 tokenizer indexes a whole CJK run as one token, so
        # a MATCH for a segmented term returns nothing.  Segmenting the query
        # without skipping that pre-filter would have made this plane strictly
        # worse than leaving it unsegmented.
        store = TeamMemoryStore(Path(self.temp.name) / "cjk.sqlite3")
        store.publish({
            "type": "discovery",
            "title": "李宁的调度链验证",
            "content": "李宁完成了 rlvr-auto-survey 的 octo-loop 调度链验证。",
            "status": "active",
        }, default_status="active")

        result = store.search("李宁最近在做什么")
        self.assertFalse(result["abstain"])
        self.assertEqual(result["active"][0]["title"], "李宁的调度链验证")
        self.assertIn("李宁", result["diagnostics"]["query_terms"])
        # The ASCII path must keep using the index it can actually match on.
        self.assertFalse(store.search("rlvr-auto-survey")["abstain"])

    def test_one_session_keeps_its_fullest_candidate_instead_of_stacking(self):
        # Autocapture fires once per assistant turn.  A single conversation
        # therefore deposits one candidate per turn, and those turns are
        # semantically redundant but lexically too varied to dedupe by content
        # (a real 42-minute thread ran 0.19-0.34 pairwise token overlap, under
        # the 0.72 scored by two genuinely different answers).  Provenance is
        # the signal that works: same agent, same session, same kind.
        store = TeamMemoryStore(Path(self.temp.name) / "session.sqlite3")

        def capture(content: str, *, session: str, memory_type: str = "discovery", agent: str = "yaole"):
            """Stand in for the capture path in core.py."""
            representative = store.session_representative(author_agent=agent, session=session, memory_type=memory_type)
            if representative is not None and int(representative["length"]) >= len(content):
                return None, representative["id"]
            supersedes = representative["id"] if representative else None
            published = store.publish({
                "type": memory_type,
                "title": content[:40],
                "content": content,
                "provenance": {"agent": agent, "session": session},
                "supersedes": supersedes,
            })
            return published["memory"]["id"], supersedes

        first, none_yet = capture("不能打包或发送本地 access token。", session="s-1")
        self.assertIsNone(none_yet)
        second, superseded = capture("私发也不行,我不能导出本机 access token 或 Cookie,这类值只交换名字。", session="s-1")
        self.assertEqual(superseded, first)

        # The earlier turn is retired, not deleted: content survives for audit
        # and the live candidate is the fuller one.
        self.assertEqual(store.get(first)["result"]["status"], "superseded")
        self.assertIn("access token", store.get(first)["result"]["content"])
        self.assertEqual(store.get(second)["result"]["status"], "candidate")
        # A superseded row must leave the answer surface.
        self.assertNotIn(first, [item["id"] for item in store.search("access token")["candidates"]])

        # Sessions were measured to arrive longest-first and decay into wrap-up
        # remarks, so a later, shorter turn is not a refinement.  It must not
        # retire the substantive row -- that inversion is what made an earlier
        # recency rule retire the best row in four of seven real storms.
        wrapup, stands = capture("好的,收到。", session="s-1")
        self.assertIsNone(wrapup)
        self.assertEqual(stands, second)
        self.assertEqual(store.get(second)["result"]["status"], "candidate")

        # A different session is a different conversation; it must not collapse.
        other, other_supersedes = capture("这台机器的 SSH 服务未启用,22 端口未监听。", session="s-2")
        self.assertIsNone(other_supersedes)
        self.assertEqual(store.get(other)["result"]["status"], "candidate")
        self.assertEqual(store.get(second)["result"]["status"], "candidate")

        # Same session but a different kind is a different claim.
        decision, decision_supersedes = capture("决定:凭据只交换名字,不交换值。", session="s-1", memory_type="decision")
        self.assertIsNone(decision_supersedes)
        self.assertEqual(store.get(decision)["result"]["status"], "candidate")

    def test_session_collapse_never_retracts_a_reviewed_record(self):
        # Superseding an activated record would silently withdraw knowledge a
        # human already accepted, so review is the hard floor for this collapse.
        store = TeamMemoryStore(Path(self.temp.name) / "reviewed.sqlite3")
        reviewed = store.publish({
            "type": "discovery",
            "title": "网关鉴权",
            "content": "H3 网关配置 H3_API_KEY 后对 /v1/* 强制 Bearer。",
            "provenance": {"agent": "yaole", "session": "s-9"},
        })["memory"]["id"]
        store.activate(reviewed, reviewer="武垚乐")

        self.assertIsNone(store.session_representative(author_agent="yaole", session="s-9", memory_type="discovery"))
        self.assertEqual(store.get(reviewed)["result"]["status"], "active")

        # A record with no session carries no signal at all, so it is left alone.
        self.assertIsNone(store.session_representative(author_agent="yaole", session=None, memory_type="discovery"))
        self.assertIsNone(store.session_representative(author_agent=None, session="s-9", memory_type="discovery"))

    def test_collapse_session_candidates_is_a_dry_run_until_applied(self):
        # The same rule the capture path now enforces, applied to rows captured
        # before it existed.  It must never fire without being asked.
        store = TeamMemoryStore(Path(self.temp.name) / "collapse.sqlite3")
        # Shaped like a real storm: the substantive turn lands first and the
        # session decays into wrap-up remarks.
        lengths = [700, 90, 90, 120]
        ids = []
        for index, length in enumerate(lengths):
            ids.append(store.publish({
                "type": "discovery",
                "title": f"turn {index}",
                "content": f"第 {index} 轮:" + "详" * length,
                "provenance": {"agent": "yaole", "session": "s-storm"},
            })["memory"]["id"])
        substantive = ids[0]
        keeper = store.publish({
            "type": "discovery",
            "title": "another session",
            "content": "另一个会话里的独立发现。",
            "provenance": {"agent": "yaole", "session": "s-other"},
        })["memory"]["id"]
        orphan = store.publish({
            "type": "discovery",
            "title": "no session",
            "content": "没有会话来源的记录,没有信号可用。",
            "provenance": {"agent": "yaole"},
        })["memory"]["id"]

        dry = store.collapse_session_candidates()
        self.assertFalse(dry["applied"])
        self.assertEqual(dry["collapsed"], 3)
        self.assertEqual(dry["candidates_without_session"], 1)
        # Nothing moved on a dry run.
        self.assertEqual(store.get(ids[1])["result"]["status"], "candidate")

        applied = store.collapse_session_candidates(apply=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["collapsed"], 3)
        # The fullest turn stands even though three later turns follow it.
        self.assertEqual(store.get(substantive)["result"]["status"], "candidate")
        for retired in ids[1:]:
            self.assertEqual(store.get(retired)["result"]["status"], "superseded")
            self.assertEqual(store.get(retired)["result"]["superseded_by"], substantive)
            # Retired, not deleted.
            self.assertTrue(store.get(retired)["result"]["content"])
        self.assertEqual(store.get(keeper)["result"]["status"], "candidate")
        self.assertEqual(store.get(orphan)["result"]["status"], "candidate")

        # Idempotent: a second pass has nothing left to collapse.
        self.assertEqual(store.collapse_session_candidates(apply=True)["collapsed"], 0)

    def test_an_unreachable_carved_fragment_is_dropped_from_the_requirement(self):
        # The builtin n-gram path still manufactures fragments no document
        # contains, so the corpus probe that drops them stays live.  Terms are
        # passed explicitly here: this is a property of claim support, not of
        # whichever tokenizer happens to be installed.
        terms = ["octo-daemon", "健康监控", "康监"]
        excerpt = "octo-daemon 的健康监控 cron 每小时跑一次。"
        support = _claim_support(terms, excerpt, 1, 1, unreachable=frozenset({"康监"}))
        self.assertEqual(support["claim_support"], "direct")

        absent = _claim_support(["octo-daemon", "健康监控", "凭据轮换"], excerpt, 1, 1)
        self.assertEqual(absent["claim_support"], "partial")
        self.assertIn("凭据轮换", absent["unmatched_terms"])

    def test_a_term_the_user_delimited_stays_required_when_the_corpus_lacks_it(self):
        # This is the case where abstaining is right, and it is what keeps a
        # query naming something absent from being answered anyway.
        terms = query_terms("ZZZQWE 虚构项目最近进展")
        support = _claim_support(terms, "本周完成了模型评审。", 1, 1, unreachable=frozenset(terms))
        self.assertNotEqual(support["claim_support"], "direct")
        self.assertIn("zzzqwe", support["unmatched_terms"])

    def test_the_citation_path_counts_as_evidence_for_claim_support(self):
        # Retrieval indexes "<path> <text>", so a document can be found *by*
        # its path and then be unable to prove the term that found it.
        # This is the live shape: the excerpt window carried the repository name
        # and nothing else the query asked for, because a per-person layout keeps
        # the person and the section in the filename.
        terms = query_terms("rlvr-auto-survey standup 李宁")
        excerpt = "## 2026-08-18\n\n- 完成 rlvr-auto-survey 的 octo-loop 调度链验证。"
        without_path = _claim_support(terms, excerpt, 1, 3)
        self.assertEqual(without_path["claim_support"], "partial")
        self.assertEqual(sorted(without_path["unmatched_terms"]), ["standup", "李宁"])
        with_path = _claim_support(terms, excerpt, 1, 3, path="rlvr-auto-survey/standup/李宁.md")
        self.assertEqual(with_path["claim_support"], "direct")
        # Spans stay excerpt-only: a path match has no line to point at.
        self.assertTrue(all(span["line_start"] >= 1 for span in with_path["supporting_spans"]))

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

    def test_explicit_local_links_expand_relationship_queries(self):
        docs = self.alpha / "docs"
        (docs / "alpha.md").write_text(
            "# Alpha entity\n\nRelated benchmark: [Beta benchmark](benchmark.md)\n",
            encoding="utf-8",
        )
        (docs / "benchmark.md").write_text(
            "# Beta benchmark\n\nA result only described by the linked entity.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.alpha), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "links"], check=True)
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})

        result = core.search(None, "Alpha related benchmark", local=True)

        self.assertFalse(result["abstain"])
        self.assertEqual(result["verified"][0]["path"], "docs/alpha.md")
        self.assertTrue(any(item["path"] == "docs/benchmark.md" for item in result["verified"][0]["related"]))

    def test_latest_routing_uses_dates_in_report_headings(self):
        reports = self.alpha / "reports"
        reports.mkdir()
        (reports / "current.md").write_text("# 2026-08-09\n\nExperiment current result.\n", encoding="utf-8")
        (reports / "older.md").write_text("# 2026-07-01\n\nExperiment older result.\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.alpha), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "dated reports"], check=True)
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})

        result = core.search(None, "最近 experiment", local=True)

        self.assertFalse(result["abstain"])
        self.assertEqual(result["verified"][0]["path"], "reports/current.md")

    def test_remote_snapshot_does_not_use_dirty_worktree(self):
        bare = Path(self.temp.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "remote", "add", "origin", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "push", "-q", "-u", "origin", "main"], check=True)
        (self.alpha / "docs" / "atlas.md").write_text("# Local uncommitted secret\n", encoding="utf-8")
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
        view = prepare_view(SourceSpec("alpha", self.alpha, "alpha"))
        self.assertEqual(view.commit_type, "remote_snapshot")
        self.assertEqual(view.commit, commit)
        self.assertNotEqual(view.path, self.alpha)
        self.assertFalse(view.dirty)
        self.assertIn("Atlas evidence", (view.path / "docs" / "atlas.md").read_text(encoding="utf-8"))
        self.assertIn("Local uncommitted", (self.alpha / "docs" / "atlas.md").read_text(encoding="utf-8"))

    def test_snapshot_repairs_a_torn_checkout(self):
        # A checkout killed mid-write (a caller timeout is enough) leaves the
        # snapshot's index and HEAD advanced while files on disk stay old --
        # after which a plain checkout sees nothing to do and the staleness is
        # permanent.  Measured live: a snapshot labelled one commit served
        # standup files from a week earlier and every question about that week
        # abstained against a corpus that had the answer.  prepare_view must
        # re-materialize by force and only claim a commit it verified.
        bare = Path(self.temp.name) / "torn-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "remote", "add", "origin", str(bare)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "push", "-q", "-u", "origin", "main"], check=True)
        view = prepare_view(SourceSpec("alpha", self.alpha, "alpha"))
        self.assertEqual(view.commit_type, "remote_snapshot")
        # Simulate the torn state: worktree content diverges from the commit
        # the snapshot claims, exactly what an interrupted checkout leaves.
        (view.path / "docs" / "atlas.md").write_text("stale bytes from last week\n", encoding="utf-8")
        repaired = prepare_view(SourceSpec("alpha", self.alpha, "alpha"))
        self.assertEqual(repaired.commit_type, "remote_snapshot")
        self.assertEqual(repaired.commit, view.commit)
        self.assertIn("Atlas evidence", (repaired.path / "docs" / "atlas.md").read_text(encoding="utf-8"))

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

        before_status = subprocess.check_output(["git", "-C", str(self.alpha), "status", "--porcelain"], text=True, encoding="utf-8")
        feedback = core.feedback(self.alpha, "alpha:docs/atlas.md", "useful", "up")
        candidate_input = Path(self.temp.name) / "candidate.json"
        candidate_input.write_text(json.dumps({"title": "Candidate", "content": "Pending evidence"}), encoding="utf-8")
        promoted = core.promote(self.alpha, str(candidate_input))
        after_status = subprocess.check_output(["git", "-C", str(self.alpha), "status", "--porcelain"], text=True, encoding="utf-8")
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

    def test_team_memory_import_replays_feedback(self):
        """Cross-machine feedback import recalculates confidence/status; duplicate is idempotent."""
        a_path = Path(self.temp.name) / "feedback-a.sqlite3"
        node_a = TeamMemoryStore(a_path, node_id="node-a")
        pub = node_a.publish({
            "type": "decision", "title": "Import replay target",
            "content": "Used to verify feedback replay on cross-machine import.",
            "status": "active", "confidence": 0.9,
        }, default_status="active")
        mid = pub["memory"]["id"]
        self.assertEqual(node_a.get(mid)["result"]["status"], "active")
        self.assertAlmostEqual(node_a.get(mid)["result"]["confidence"], 0.9)

        # Feedbacks that should reduce confidence / trigger lifecycle transitions
        node_a.feedback(mid, "wrong", "rejected on A", agent="reviewer-a", feedback_id="fb-1")
        a_after = node_a.get(mid)["result"]
        self.assertEqual(a_after["status"], "stale")
        self.assertAlmostEqual(a_after["confidence"], 0.8)
        bundle = node_a.export_bundle()

        # Machine B: same initial state, import bundle → should replay transition
        b_path = Path(self.temp.name) / "feedback-b.sqlite3"
        node_b = TeamMemoryStore(b_path, node_id="node-b")
        pub_b = node_b.publish({
            "type": "decision", "title": "Import replay target",
            "content": "Used to verify feedback replay on cross-machine import.",
            "status": "active", "confidence": 0.9,
        }, default_status="active")
        self.assertEqual(pub_b["memory"]["id"], mid)
        self.assertEqual(node_b.get(mid)["result"]["status"], "active")
        self.assertAlmostEqual(node_b.get(mid)["result"]["confidence"], 0.9)

        result = node_b.import_bundle(bundle)
        self.assertEqual(result["imported"]["feedback_added"], 1)
        self.assertEqual(result["imported"]["feedback_replayed"], 1)
        b_after = node_b.get(mid)["result"]
        self.assertEqual(b_after["status"], "stale")
        self.assertAlmostEqual(b_after["confidence"], 0.8)

        # Duplicate import must not double-de-rank
        dup = node_b.import_bundle(bundle)
        self.assertEqual(dup["imported"]["feedback_added"], 0)
        self.assertEqual(dup["imported"]["feedback_replayed"], 0)
        b_dup = node_b.get(mid)["result"]
        self.assertEqual(b_dup["status"], "stale")
        self.assertAlmostEqual(b_dup["confidence"], 0.8)
        self.assertEqual(b_dup["revision"], b_after["revision"])

        # "stale" rating with 2 agents triggers status transition on import
        c_path = Path(self.temp.name) / "feedback-c.sqlite3"
        node_c = TeamMemoryStore(c_path, node_id="node-c")
        node_c.publish({
            "type": "decision", "title": "Stale-by-import",
            "content": "Will receive stale feedback via import from two agents.",
            "status": "active", "confidence": 0.9,
        }, default_status="active")

        bundle_a = TeamMemoryStore(Path(self.temp.name) / "fb-stale-a.sqlite3", node_id="agent-a").publish({
            "type": "decision", "title": "Stale-by-import",
            "content": "Will receive stale feedback via import from two agents.",
            "status": "active", "confidence": 0.9,
        }, default_status="active")
        # Build a bundle with "stale" feedback from agent-x
        tmp_x_path = Path(self.temp.name) / "tmp-x.sqlite3"
        node_x = TeamMemoryStore(tmp_x_path, node_id="node-x")
        node_x.publish({
            "type": "decision", "title": "Stale-by-import",
            "content": "Will receive stale feedback via import from two agents.",
            "status": "active", "confidence": 0.9,
        }, default_status="active")
        node_x.feedback(bundle_a["memory"]["id"], "stale", "old", agent="agent-x", feedback_id="sfb-1")
        r1 = node_c.import_bundle(node_x.export_bundle())
        self.assertEqual(r1["imported"]["feedback_replayed"], 1)
        c_after_1 = node_c.get(bundle_a["memory"]["id"])["result"]
        self.assertEqual(c_after_1["status"], "active")  # only 1 agent, not stale yet
        self.assertAlmostEqual(c_after_1["confidence"], 0.8)

        tmp_y_path = Path(self.temp.name) / "tmp-y.sqlite3"
        node_y = TeamMemoryStore(tmp_y_path, node_id="node-y")
        node_y.publish({
            "type": "decision", "title": "Stale-by-import",
            "content": "Will receive stale feedback via import from two agents.",
            "status": "active", "confidence": 0.9,
        }, default_status="active")
        node_y.feedback(bundle_a["memory"]["id"], "stale", "also old", agent="agent-y", feedback_id="sfb-2")
        r2 = node_c.import_bundle(node_y.export_bundle())
        self.assertEqual(r2["imported"]["feedback_replayed"], 1)
        c_after_2 = node_c.get(bundle_a["memory"]["id"])["result"]
        self.assertEqual(c_after_2["status"], "stale")  # 2 agents → stale
        self.assertAlmostEqual(c_after_2["confidence"], 0.7)

    def test_team_memory_activation_and_causal_merge_conflict(self):
        base_path = Path(self.temp.name) / "base.sqlite3"
        node_a = TeamMemoryStore(base_path, node_id="node-a")
        candidate = node_a.publish({
            "id": "team:decision:causal",
            "type": "decision",
            "title": "Causal decision",
            "content": "Use one isolated worktree per issue.",
            "status": "candidate",
            "author_agent": "agent-a",
        })
        memory_id = candidate["memory"]["id"]
        candidate_bundle = node_a.export_bundle()
        activated = node_a.activate(memory_id, reviewer="reviewer-a")
        self.assertEqual(activated["status"], "active")
        self.assertEqual(activated["memory"]["revision"], 2)
        self.assertEqual(activated["memory"]["parent_revision"], "node-a:1")
        self.assertEqual(activated["memory"]["author_agent"], "agent-a")
        self.assertEqual(activated["memory"]["reviewed_by"], "reviewer-a")
        self.assertEqual(activated["memory"]["activated_at"], activated["memory"]["updated_at"])

        node_b = TeamMemoryStore(Path(self.temp.name) / "node-b.sqlite3", node_id="node-b")
        base_bundle = node_a.export_bundle()
        self.assertEqual(base_bundle["schema_version"], 3)
        self.assertEqual(node_b.import_bundle(base_bundle)["imported"]["inserted"], 1)
        lagging = TeamMemoryStore(Path(self.temp.name) / "lagging.sqlite3", node_id="node-lagging")
        lagging.import_bundle(candidate_bundle)
        node_a.feedback(memory_id, "wrong", "A rejected it", agent="reviewer-a")
        fast_forward = lagging.import_bundle(node_a.export_bundle())
        self.assertEqual(fast_forward["imported"]["updated"], 1)
        self.assertEqual(lagging.get(memory_id)["result"]["revision"], 3)
        self.assertEqual(lagging.get(memory_id)["result"]["status"], "stale")
        node_b.feedback(memory_id, "stale", "B has not confirmed expiry", agent="reviewer-b")
        conflict = node_a.import_bundle(node_b.export_bundle())
        self.assertEqual(conflict["imported"]["conflicts"], 1)
        self.assertEqual(node_a.get(memory_id)["result"]["status"], "stale")
        feedback = node_a.feedback(memory_id, "helpful", "stable feedback", agent="reviewer-a", feedback_id="feedback-1")
        duplicate_feedback = node_a.feedback(memory_id, "helpful", "stable feedback", agent="reviewer-a", feedback_id="feedback-1")
        self.assertFalse(feedback["duplicate"])
        self.assertTrue(duplicate_feedback["duplicate"])

        receiver = TeamMemoryStore(Path(self.temp.name) / "receiver.sqlite3", node_id="node-r")
        receiver.import_bundle(base_bundle)
        child = TeamMemoryStore(Path(self.temp.name) / "child.sqlite3", node_id="node-c")
        child.import_bundle(base_bundle)
        child.feedback(memory_id, "wrong", "causal child", agent="reviewer-c")
        applied = receiver.import_bundle(child.export_bundle())
        self.assertEqual(applied["imported"]["updated"], 1)
        self.assertEqual(receiver.get(memory_id)["result"]["status"], "stale")

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
        command = [
            sys.executable,
            str(SCRIPTS / "repository-memory.py"),
            "team-evaluate",
            "--records", str(root / "eval/public/team_memory/records.jsonl"),
            "--queries", str(root / "eval/public/team_memory/queries.jsonl"),
            "--qrels", str(root / "eval/public/team_memory/qrels.jsonl"),
            "--gate", "--json",
        ]
        gated = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False)
        self.assertEqual(gated.returncode, 0, gated.stderr)
        self.assertTrue(json.loads(gated.stdout)["gate"]["passed"])

    def test_team_memory_migrates_legacy_database_and_backfills_revision_log(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL, summary TEXT NOT NULL, scope TEXT NOT NULL,
                provenance TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL,
                supersedes TEXT, superseded_by TEXT, valid_from TEXT, valid_until TEXT,
                author_agent TEXT, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE memory_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
                rating TEXT NOT NULL, note TEXT NOT NULL, agent TEXT, created_at TEXT NOT NULL
            );
        """)
        connection.execute("INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            "team:legacy:1", "decision", "Legacy", "Legacy content", "Legacy content", "{}", "{}", 0.5, "active", None, None, None, None, "agent-a", None, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        ))
        connection.commit()
        connection.close()
        store = TeamMemoryStore(path, node_id="node-migrated")
        value = store.get("team:legacy:1")["result"]
        bundle = store.export_bundle()
        self.assertEqual(value["revision"], 1)
        self.assertEqual(value["origin_node"], "legacy")
        self.assertIsNone(value["reviewed_by"])
        self.assertEqual(bundle["schema_version"], 3)
        self.assertEqual(len(bundle["revisions"]), 1)


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
        ]
        process = subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, encoding="utf-8", capture_output=True, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertIn("2026-07-28", responses[0]["result"]["supportedVersions"])
        self.assertEqual(responses[0]["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "repository-memory")
        self.assertEqual(responses[0]["result"]["resultType"], "complete")
        self.assertEqual(responses[1]["result"]["resultType"], "complete")
        self.assertEqual({tool["name"] for tool in responses[1]["result"]["tools"]}, {"memory_doctor", "memory_sync", "memory_search", "memory_get", "memory_timeline", "memory_observe", "memory_reflect"})
        payload = responses[2]["result"]["structuredContent"]
        self.assertEqual(responses[2]["result"]["resultType"], "complete")
        self.assertIn("verified", payload)
        self.assertIn("candidates", payload)
        self.assertFalse(payload["abstain"])
        self.assertEqual({item["source"] for item in payload["verified"]}, {"alpha"})
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
        process = subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, encoding="utf-8", capture_output=True, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[1]["result"]["resultType"], "complete")

    def test_mcp_rejects_unknown_per_request_protocol(self):
        command = [sys.executable, str(SCRIPTS / "repository-memory.py"), "mcp", "--root", str(self.alpha)]
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}}}
        process = subprocess.run(command, input=json.dumps(request) + "\n", text=True, encoding="utf-8", capture_output=True, check=True)
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
        environment = {**os.environ, "REPOSITORY_MEMORY_AGENT_ID": "yaole"}
        process = subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, encoding="utf-8", capture_output=True, check=True, env=environment)
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
        self.assertEqual(search_request["agent"], "yaole")
        self.assertNotIn("Atlas evidence", audit_log.read_text(encoding="utf-8"))
        self.assertEqual(search_response["verified_count"], 1)
        self.assertEqual(search_response["protocol_version"], "2026-07-28")
        self.assertEqual(search_response["agent"], "yaole")

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
        subprocess.run(command, input="\n".join(json.dumps(item) for item in requests) + "\n", text=True, encoding="utf-8", capture_output=True, check=True)
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
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
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

        remote = {**verified, "citation": {**verified["citation"], "commit": "remote-commit", "commit_type": "remote_snapshot", "evidence": "Atlas evidence"}}
        with patch("evaluate.search", return_value={"verified": [remote], "candidates": [], "abstain": False, "mode": "exact"}):
            remote_report = evaluate_queries(self.alpha, queries, qrels, local=False)
        self.assertEqual(remote_report["citation_parseability"], 1.0)

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
        self.assertEqual(normalized["memory"], {"layer": "L1", "type": "atomic", "role": None, "query_source": "memorycore", "strategy": "keyword"})
        self.assertTrue(normalized["citation"]["valid"])
        self.assertEqual(normalized["repository"], "alpha")
        self.assertEqual(normalized["layer"], "L1")
        self.assertEqual(normalized["status"], "secondary")
        self.assertEqual(normalized["readback"]["receipt"], "repository-citation-readback")
        self.assertEqual(normalized["provenance"]["repository"], "alpha")

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

    def test_memorycore_doctor_reports_empty_layers_without_claiming_population(self):
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
        for layer in ("L0", "L1", "L2", "L3"):
            state = report["layers"][layer]
            self.assertEqual(state["capability"], "supported")
            self.assertEqual(state["api_status"], "ready")
            self.assertEqual(state["population"], "empty")
            self.assertEqual(state["readback"], "verified")

    def test_memorycore_health_reports_gateway_embedding_capability(self):
        config = MemoryCoreConfig(endpoint="http://127.0.0.1:8420", api_key=None, team_id="team", agent_id="agent", user_id="user")
        client = MemoryCoreClient(config)
        with patch.object(client, "_request", return_value={"code": 0, "data": {"status": "ok", "stores": {"vectorStore": True, "embeddingService": True}}}):
            report = client.health(refresh=True)
        self.assertTrue(report["embedding"]["available"])
        self.assertEqual(report["embedding"]["strategy"], "hybrid")
        self.assertTrue(report["server_stores"]["embedding_service"])

    def test_memorycore_get_pages_until_l0_record_is_found(self):
        config = MemoryCoreConfig(endpoint="http://127.0.0.1:8420", api_key=None, team_id="team", agent_id="agent", user_id="user")
        client = MemoryCoreClient(config)

        def request(method, path, body=None):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v3/conversation/query")
            offset = body.get("offset", 0)
            if offset == 0:
                return {"code": 0, "data": {"messages": [{"id": "msg-first", "content": "first"}], "total": 101}}
            return {"code": 0, "data": {"messages": [{"id": "msg-late", "content": "late evidence"}], "total": 101}}

        with patch.object(client, "_request", side_effect=request):
            result = client.get("memorycore:L0:msg-late")
        self.assertEqual(result["memory"]["content"], "late evidence")
        self.assertEqual(result["citation"]["memory_id"], "msg-late")
        self.assertTrue(result["citation"]["valid"])

    def test_memmy_client_preserves_local_embedding_and_layer_identity(self):
        client = MemmyClient(MemmyConfig("http://127.0.0.1:18960", True, "repository-memory", "repository-memory", "user"))
        with patch.object(client, "_request", return_value={
            "debug": {"hits": [{"id": "trace-1", "kind": "trace", "memoryLayer": "L1", "status": "activated", "snippet": "local semantic memory", "score": 0.9}]}
        }):
            results = client.search("semantic", 5)
        self.assertEqual(results[0]["id"], "memmy:L1:trace-1")
        self.assertEqual(results[0]["_memory_backend"], "memmy")
        self.assertEqual(results[0]["citation"]["source"], "memmy")

    def test_memmy_get_preserves_skill_layer_and_citation(self):
        client = MemmyClient(MemmyConfig("http://127.0.0.1:18960", True, "repository-memory", "repository-memory", "user"))
        with patch.object(client, "_request", return_value={
            "id": "skill-1",
            "kind": "skill",
            "memoryLayer": "Skill",
            "status": "resolving",
            "body": "a provider skill",
        }):
            result = client.get("memmy:Skill:skill-1")
        self.assertEqual(result["memory_layer"], "Skill")
        self.assertEqual(result["citation"]["layer"], "Skill")
        self.assertEqual(result["citation"]["memory_id"], "skill-1")

    def test_memorycore_doctor_maps_present_l0_l1_and_empty_l2_l3(self):
        config = MemoryCoreConfig(endpoint="http://127.0.0.1:8420", api_key=None, team_id="team", agent_id="agent", user_id="user")
        client = MemoryCoreClient(config)
        responses = iter([
            {"code": 0, "data": {"status": "ok"}},
            {"code": 0, "data": {"messages": [{"id": "message-1", "content": "raw"}], "total": 1}},
            {"code": 0, "data": {"items": [{"id": "atomic-1", "content": "fact"}], "total": 1}},
            {"code": 0, "data": {"entries": [], "total": 0}},
            {"code": 0, "data": {"content": ""}},
        ])
        with patch.object(client, "_request", side_effect=lambda *_args, **_kwargs: next(responses)):
            layers = client.health(refresh=True, probe_layers=True)["layers"]
        self.assertEqual(layers["L0"]["population"], "present")
        self.assertEqual(layers["L0"]["record_count"], 1)
        self.assertEqual(layers["L1"]["population"], "present")
        self.assertEqual(layers["L1"]["record_count"], 1)
        self.assertEqual(layers["L2"]["population"], "empty")
        self.assertEqual(layers["L3"]["population"], "empty")

    def test_memorycore_doctor_preserves_pending_and_unknown_responses(self):
        config = MemoryCoreConfig(endpoint="http://127.0.0.1:8420", api_key=None, team_id="team", agent_id="agent", user_id="user")
        client = MemoryCoreClient(config)
        responses = iter([
            {"code": 0, "data": {"status": "ok"}},
            {"code": 0, "data": {"messages": [], "total": 0}},
            {"code": 0, "data": {"status": "pending", "items": [], "total": 0}},
            {"code": 0, "data": {}},
            {"code": 0, "data": {"status": "pending", "content": ""}},
        ])
        with patch.object(client, "_request", side_effect=lambda *_args, **_kwargs: next(responses)):
            layers = client.health(refresh=True, probe_layers=True)["layers"]
        self.assertEqual(layers["L1"]["population"], "empty")
        self.assertEqual(layers["L1"]["readback"], "pending")
        self.assertEqual(layers["L2"]["api_status"], "ready")
        self.assertEqual(layers["L2"]["population"], "unknown")
        self.assertEqual(layers["L2"]["readback"], "unknown")
        self.assertEqual(layers["L3"]["population"], "empty")
        self.assertEqual(layers["L3"]["readback"], "pending")

    def test_memorycore_doctor_marks_layer_population_unknown_when_unreachable(self):
        config = MemoryCoreConfig(endpoint="http://127.0.0.1:8420", api_key=None, team_id="team", agent_id="agent", user_id="user")
        client = MemoryCoreClient(config)
        with patch.object(client, "_request", side_effect=MemoryCoreError("service unavailable")):
            report = client.health(refresh=True, probe_layers=True)
        self.assertEqual(report["status"], "unreachable")
        for state in report["layers"].values():
            self.assertEqual(state["capability"], "supported")
            self.assertEqual(state["api_status"], "unreachable")
            self.assertEqual(state["population"], "unknown")
            self.assertEqual(state["readback"], "unknown")

    def test_memorycore_doctor_isolates_one_unreachable_layer_api(self):
        config = MemoryCoreConfig(endpoint="http://127.0.0.1:8420", api_key=None, team_id="team", agent_id="agent", user_id="user")
        client = MemoryCoreClient(config)
        responses = iter([
            {"code": 0, "data": {"status": "ok"}},
            {"code": 0, "data": {"messages": [], "total": 0}},
            MemoryCoreError("atomic API unavailable"),
            {"code": 0, "data": {"entries": [], "total": 0}},
            {"code": 0, "data": {"content": ""}},
        ])

        def respond(*_args, **_kwargs):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(client, "_request", side_effect=respond):
            layers = client.health(refresh=True, probe_layers=True)["layers"]
        self.assertEqual(layers["L0"]["api_status"], "ready")
        self.assertEqual(layers["L1"]["api_status"], "unreachable")
        self.assertEqual(layers["L1"]["population"], "unknown")
        self.assertEqual(layers["L1"]["readback"], "unknown")
        self.assertEqual(layers["L2"]["api_status"], "ready")
        self.assertEqual(layers["L3"]["api_status"], "ready")

    def test_document_verification_is_independent_from_claim_coverage(self):
        composite = self.alpha / "docs" / "composite.md"
        composite.write_text("# Composite\nAtlas and alpha are introduced here.\n" + ("context line\n" * 30) + "beta is documented in the same record.\n", encoding="utf-8")
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
        self.assertTrue(result["abstain"])
        self.assertEqual(result["answerable"], [])
        self.assertTrue(result["diagnostics"]["claim_abstain"])

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
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
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

    def test_get_can_pin_search_line_window(self):
        document = self.alpha / "docs" / "long.md"
        document.write_text("\n".join([f"line {index}" for index in range(1, 31)]) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.alpha), "add", str(document)], check=True)
        subprocess.run(["git", "-C", str(self.alpha), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "long"], check=True)
        self.write_config({"sources": [{"id": "alpha", "root": str(self.alpha)}]})
        fetched = core.get_result(self.alpha, "alpha:docs/long.md", line_start=21, line_end=23)
        self.assertTrue(fetched["found"])
        self.assertEqual(fetched["result"]["citation"]["line_start"], 21)
        self.assertEqual(fetched["result"]["citation"]["line_end"], 23)
        self.assertEqual(fetched["result"]["evidence_window"], {"line_start": 21, "line_end": 23, "requested_line_start": 21, "requested_line_end": 23, "truncated": True})
        self.assertEqual(fetched["result"]["excerpt"], "line 21\nline 22\nline 23")

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
            self.assertEqual(memory["verified"][0]["layer"], "L0")
            self.assertEqual(memory["verified"][0]["status"], "verified")
            self.assertTrue(memory["verified"][0]["readback"]["verified"])
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
                    {"id": "alpha", "workspace": str(workspaces[0]), "skills": ["legacy-memory"], "tools": {"alsoAllow": []}},
                    {"id": "beta", "workspace": str(workspaces[1])},
                ]
            },
            "plugins": {"entries": {"legacy-memory-autocapture": {"enabled": True, "config": {"guardEnabled": True}}}},
        }), encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "HOME": str(machine),
            "XDG_DATA_HOME": str(machine / "data"),
            "XDG_CONFIG_HOME": str(machine / "config"),
            "XDG_CACHE_HOME": str(machine / "cache"),
            "CODEX_HOME": str(machine / ".codex"),
            "CLAUDE_CONFIG_DIR": str(machine / ".claude"),
            # The names a deployment is upgrading *from* are supplied by that
            # deployment, so the installer reads them from the environment and
            # defaults to none.  Setting them here is what a site upgrading an
            # older install does, and it is what makes the two assertions below
            # exercise the migration path rather than an empty set.
            "REPOSITORY_MEMORY_LEGACY_SKILL_NAMES": "legacy-memory",
            "REPOSITORY_MEMORY_LEGACY_OPENCLAW_PLUGIN_IDS": "legacy-memory-autocapture",
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
        ], text=True, encoding="utf-8", capture_output=True, env=environment)
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
        ], text=True, encoding="utf-8", capture_output=True, check=True, env=environment)
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
        self.assertFalse(configured["plugins"]["entries"]["legacy-memory-autocapture"]["enabled"])
        alpha = next(agent for agent in configured["agents"]["list"] if agent["id"] == "alpha")
        beta = next(agent for agent in configured["agents"]["list"] if agent["id"] == "beta")
        self.assertIn("repository-memory", alpha["skills"])
        self.assertNotIn("legacy-memory", alpha["skills"])
        self.assertIn("repository-memory__memory_search", alpha["tools"]["alsoAllow"])
        self.assertIn("repository_memory_search", alpha["tools"]["alsoAllow"])
        self.assertIn("repository_memory_doctor", alpha["tools"]["alsoAllow"])
        self.assertIn("repository_memory_get", alpha["tools"]["alsoAllow"])
        self.assertIn("repository_memory_timeline", alpha["tools"]["alsoAllow"])
        self.assertIn("repository_memory_observe", alpha["tools"]["alsoAllow"])
        self.assertIn("repository_memory_reflect", alpha["tools"]["alsoAllow"])
        self.assertNotIn("repository-memory__memory_context", alpha["tools"]["alsoAllow"])
        self.assertNotIn("repository-memory__memory_publish", alpha["tools"]["alsoAllow"])
        self.assertNotIn("repository-memory", beta.get("skills", []))
        self.assertNotIn("repository-memory__memory_search", beta.get("tools", {}).get("alsoAllow", []))
        self.assertNotIn("repository_memory_search", beta.get("tools", {}).get("alsoAllow", []))
        wrapper = machine / ".local" / "bin" / ("repository-memory.cmd" if os.name == "nt" else "repository-memory")
        self.assertTrue(wrapper.is_file())
        searched = subprocess.run([
            str(wrapper),
            "search",
            "Atlas evidence",
            "--scope",
            "repository",
            "--local",
            "--json",
        ], text=True, encoding="utf-8", capture_output=True, check=True, env=environment)
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
        empty_memory = core.doctor(None)["memory"]
        self.assertEqual(empty_memory["layers"]["L0"]["population"], "empty")
        self.assertEqual(empty_memory["layers"]["L1"]["population"], "empty")
        self.assertEqual(empty_memory["layers"]["L2"]["capability"], "supported")
        self.assertEqual(empty_memory["layers"]["L2"]["population"], "empty")
        session = Path(self.temp.name) / "local-session.json"
        session.write_text(json.dumps({
            "session_id": "local-session",
            "messages": [{"role": "user", "content": "remember portable local memory"}],
        }), encoding="utf-8")
        ingested = core.ingest_session(None, str(session))
        self.assertTrue(ingested["ok"])
        self.assertEqual(ingested["source"], "standalone-memory")
        self.assertEqual(ingested["memory"]["supported_layers"], ["L0", "L1", "L2", "L3"])
        self.assertEqual(ingested["memory"]["layers"]["L0"]["population"], "present")
        self.assertEqual(ingested["memory"]["layers"]["L1"]["population"], "present")
        found = core.search(None, "portable local memory", scope="memory")
        self.assertFalse(found["abstain"])
        self.assertEqual(found["verified"][0]["source"], "standalone-memory")
        self.assertEqual(found["verified"][0]["memory"]["layer"], "L1")
        self.assertEqual(found["verified"][0]["tier"], 1)
        self.assertEqual(found["verified"][0]["ref_kind"], "trace")
        fetched = core.get_result(None, found["verified"][0]["id"])
        self.assertTrue(fetched["found"])
        self.assertEqual(fetched["source"], "standalone-memory")
        self.assertFalse(subprocess.check_output(["git", "-C", str(self.alpha), "status", "--porcelain"], text=True, encoding="utf-8"))

        timeline = core._mcp_dispatch("memory_timeline", {"session_id": "local-session"})
        self.assertTrue(timeline["ok"])
        self.assertGreaterEqual(timeline["count"], 2)
        self.assertEqual({event["layer"] for event in timeline["events"]}, {"L0", "L1"})

        observed = core._mcp_dispatch("memory_observe", {"session_id": "local-session"})
        self.assertTrue(observed["ok"])
        self.assertEqual(observed["operation"], "observe")
        reflection = core._mcp_dispatch("memory_reflect", {"query": "portable local memory", "limit": 3})
        self.assertTrue(reflection["ok"])
        self.assertEqual(reflection["operation"], "reflect")
        self.assertEqual(reflection["status"], "candidate")
        self.assertTrue(reflection["generated"])
        self.assertFalse(reflection["accepted"])

    def test_standalone_answer_uses_the_user_question_as_a_retrieval_key(self):
        """A concise answer need not repeat the vocabulary of its question."""
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        from standalone_memory import standalone_memory_client

        session = Path(self.temp.name) / "retrieval-key-session.json"
        session.write_text(json.dumps({
            "session_id": "retrieval-key-session",
            "messages": [
                {"role": "user", "content": "凌晨批处理任务为什么失败？"},
                {"role": "assistant", "content": "连接池耗尽；上限从 10 调到 30 后恢复。"},
                {"role": "user", "content": "后来稳定吗？"},
                {"role": "assistant", "content": "连续三次回放都通过。"},
            ],
        }), encoding="utf-8")
        core.ingest_session(None, str(session))

        hits = standalone_memory_client().search("凌晨批处理任务为什么失败？", limit=3)
        answers = [item for item in hits if item.get("memory_role") == "assistant"]
        self.assertTrue(answers)
        answer = answers[0]
        self.assertIn("连接池耗尽", answer["content"])
        self.assertEqual(answer["retrieval_keys"], ["凌晨批处理任务为什么失败？"])
        self.assertEqual(answer["context"][0]["role"], "user")
        self.assertIn("凌晨批处理", answer["context"][0]["content"])
        self.assertEqual(len({item["id"] for item in hits}), len(hits))

        surfaced = core.search(None, "凌晨批处理任务为什么失败？", scope="memory")
        self.assertTrue(surfaced["abstain"], "association alone is not factual support")
        surfaced_answer = next(item for item in surfaced["verified"] if item["memory"]["role"] == "assistant")
        self.assertTrue(surfaced_answer["support"]["retrieval_key_match"])
        self.assertFalse(surfaced_answer["support"]["retrieval_key_is_evidence"])
        self.assertEqual(surfaced_answer["support"]["claim_support"], "associated")
        self.assertEqual(surfaced_answer["context_strategy"], "adjacent-session-turns")

    def test_retrieval_keys_do_not_persist_sensitive_questions(self):
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        from standalone_memory import standalone_memory_client

        client = standalone_memory_client()
        secret = "token=sk-" + "abcdefghijklmnop"
        session = Path(self.temp.name) / "sensitive-retrieval-key.json"
        session.write_text(json.dumps({"session_id": "sensitive-key", "messages": [
            {"role": "user", "content": f"为什么 {secret} 失效？"},
            {"role": "assistant", "content": "需要重新配置调用方。"},
        ]}), encoding="utf-8")
        core.ingest_session(None, str(session))
        connection = client._connect()
        try:
            row = connection.execute("SELECT metadata FROM records WHERE session_id=? AND layer='L1' AND role='assistant'", ("sensitive-key",)).fetchone()
            fts = " ".join(str(value[0]) for value in connection.execute("SELECT content FROM records_fts")) if client._fts(connection) else ""
        finally:
            connection.close()
        self.assertEqual(json.loads(row[0])["retrieval_keys"], [])
        self.assertNotIn(secret, fts)
        self.assertFalse(any(secret in " ".join(item.get("retrieval_keys") or []) for item in client.search(secret, limit=10)))

    def test_adjacent_context_is_scoped_to_one_ingest_batch(self):
        self.write_config({})
        from standalone_memory import standalone_memory_client

        client = standalone_memory_client()
        client.ingest_aml(request_id="request-one", user_id="user-a", session_id="same-session", messages=[
            {"role": "user", "content": "first batch outage cause?"},
            {"role": "assistant", "content": "pool alpha exhausted"},
        ])
        client.ingest_aml(request_id="request-two", user_id="user-a", session_id="same-session", messages=[
            {"role": "user", "content": "second batch release status?"},
            {"role": "assistant", "content": "release beta completed"},
        ])
        first = next(item for item in client.search("first batch outage cause", limit=10) if "pool alpha" in item["content"])
        second = next(item for item in client.search("second batch release status", limit=10) if "release beta" in item["content"])
        self.assertIn("first batch", first["context"][0]["content"])
        self.assertNotIn("second batch", first["context"][0]["content"])
        self.assertIn("second batch", second["context"][0]["content"])

    def test_legacy_lookup_refuses_ambiguous_same_index_rows(self):
        self.write_config({})
        from standalone_memory import standalone_memory_client

        client = standalone_memory_client()
        connection = client._connect()
        try:
            for record_id, content in (("legacy-a", "first old question"), ("legacy-b", "second old question")):
                client._upsert(connection, record_id=record_id, layer="L1", session_id="legacy-session", message_index=0, role="user", content=content, metadata={})
            connection.commit()
            rows = connection.execute("SELECT * FROM records WHERE session_id='legacy-session'").fetchall()
        finally:
            connection.close()
        self.assertEqual(client._row_lookup(rows), {})

    def test_standalone_memory_links_are_explicit_and_mmr_is_explainable(self):
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        from standalone_memory import standalone_memory_client

        client = standalone_memory_client()
        connection = client._connect()
        try:
            client._upsert(
                connection,
                record_id="local:L1:source-a",
                layer="L1",
                session_id="episode-a",
                message_index=0,
                role="assistant",
                content="explicit source evidence for a durable policy",
                metadata={},
            )
            client._upsert(
                connection,
                record_id="local:L2:policy-a",
                layer="L2",
                session_id="policy/a",
                message_index=-1,
                role="system",
                content="durable policy derived from explicit source evidence",
                metadata={"source_record_ids": ["local:L1:source-a"]},
                status="accepted",
                generated=False,
                accepted=True,
            )
            connection.commit()
        finally:
            connection.close()
        hits = client.search("durable policy", limit=3)
        self.assertTrue(hits)
        self.assertTrue(all(item["ranking"]["mmr"] for item in hits))
        policy = next(item for item in hits if item["id"] == "local:L2:policy-a")
        self.assertEqual(policy["related"][0]["id"], "local:L1:source-a")
        fetched = client.get("local:L2:policy-a")
        self.assertEqual(fetched["memory"]["related"][0]["relation"], "supports")

    def test_standalone_runtime_proves_four_layer_lifecycle_without_services(self):
        """The default install must not need a gateway or a vendor process."""
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        session = Path(self.temp.name) / "standalone-session.json"
        session.write_text(json.dumps({
            "session_id": "standalone-session",
            "messages": [{"role": "user", "content": "standalone L0 and L1 evidence"}],
        }), encoding="utf-8")
        ingested = core.ingest_session(None, str(session))
        self.assertTrue(ingested["ok"])
        self.assertEqual(ingested["source"], "standalone-memory")
        self.assertEqual(ingested["memory"]["backend"], "standalone-memory")
        self.assertEqual(ingested["memory"]["supported_layers"], ["L0", "L1", "L2", "L3"])
        self.assertTrue(ingested["memory"]["layers"]["L0"]["readback"] == "verified")
        self.assertTrue(ingested["memory"]["layers"]["L1"]["readback"] == "verified")

        candidate_input = Path(self.temp.name) / "standalone-candidate.json"
        candidate_input.write_text(json.dumps({
            "id": "standalone-policy",
            "title": "Standalone policy",
            "content": "Standalone repository memory keeps citation-first retrieval and explicit promotion.",
        }), encoding="utf-8")
        candidate = core.promote(None, str(candidate_input))
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["native_l2"][0]["status"], "candidate")
        promoted = core.promote_l3(candidate["native_l2"][0]["id"])
        self.assertTrue(promoted["verified"])
        report = core.doctor(None)
        self.assertEqual(report["active_adapter"], "standalone-memory")
        self.assertEqual(report["memory"]["layers"]["L2"]["population"], "present")
        self.assertEqual(report["memory"]["layers"]["L3"]["readback"], "verified")
        found = core.search(None, "citation-first explicit promotion", scope="memory")
        self.assertFalse(found["abstain"])
        self.assertTrue(any(item["layer"] == "L3" for item in found["verified"]))
        self.assertTrue(all(
            item["readback"]["receipt"] == "standalone-memory-readback"
            for item in found["verified"]
            if item.get("source") == "standalone-memory"
        ))

    def test_standalone_runtime_has_local_vectors_and_projects_l2_candidate(self):
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        session = Path(self.temp.name) / "vector-session.json"
        session.write_text(json.dumps({
            "session_id": "vector-session",
            "messages": [
                {"role": "user", "content": "The local retrieval policy keeps durable evidence."},
                {"role": "assistant", "content": "We will preserve that policy for future sessions."},
            ],
        }), encoding="utf-8")
        ingested = core.ingest_session(None, str(session))
        self.assertEqual(ingested["memory"]["embedding"]["strategy"], "local-hybrid")
        self.assertTrue(ingested["memory"]["embedding"]["available"])
        self.assertEqual(ingested["memory"]["layers"]["L2"]["population"], "present")
        self.assertEqual(ingested["result"]["l2_candidates"], 1)

        found = core.search(None, "durable retrieval evidence", scope="memory")
        self.assertFalse(found["abstain"])
        self.assertEqual(found["retrieval_mode"], "local-hybrid")
        self.assertTrue(found["verified"])
        self.assertTrue(any(item["layer"] == "L2" for item in found["candidates"]))

        projected = core.project_memory_candidates()
        self.assertEqual(projected["status"], "candidate")
        self.assertGreaterEqual(projected["projected"], 1)

    def test_memos_lifecycle_port_adds_episode_pool_and_feedback(self):
        self.write_config({})
        os.environ["REPOSITORY_MEMORY_AUTODISCOVER"] = "0"
        for session_id in ("episode-a", "episode-b"):
            session = Path(self.temp.name) / f"{session_id}.json"
            session.write_text(json.dumps({
                "session_id": session_id,
                "messages": [{"role": "user", "content": "repository memory timeout failure"}],
            }), encoding="utf-8")
            core.ingest_session(None, str(session))
        pool = core.evolve_memory_policies()
        self.assertGreaterEqual(pool["created"], 1)
        policy_id = pool["candidate_ids"][0]
        feedback = core.feedback(None, policy_id, "reused successfully", "helpful", "memos-feedback-1")
        self.assertTrue(feedback["ok"])
        self.assertFalse(feedback["duplicate"])
        duplicate = core.feedback(None, policy_id, "reused successfully", "helpful", "memos-feedback-1")
        self.assertTrue(duplicate["duplicate"])

    def test_memos_lifecycle_turn_and_value_contracts(self):
        self.assertEqual(classify_turn("old answer", "不对，重做") ["relation"], "revision")
        self.assertEqual(classify_turn("old answer", "换个新任务") ["relation"], "new_task")
        self.assertEqual(classify_turn("old answer", "那这个呢") ["relation"], "follow_up")
        rows = backpropagate([{"id": "a", "alpha": 0.3}, {"id": "b", "alpha": 0.3}], 1.0, now=100.0)
        self.assertEqual([row["id"] for row in rows], ["a", "b"])
        self.assertGreaterEqual(rows[-1]["priority"], rows[0]["priority"])
        self.assertEqual(len(ready_buckets([{"id": "a", "episode_id": "e1", "content": "timeout failure"}, {"id": "b", "episode_id": "e2", "content": "timeout failure"}])), 1)

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

    def test_native_l2_to_l3_promotion_is_idempotent(self):
        class FakeNative:
            configured = True

            def __init__(self):
                self.l2 = "status: generated\nlayer: L2\n\nA durable scenario"
                self.l3 = "status: accepted\nlayer: L3\n\nExisting profile"

            def get(self, _candidate_id):
                return {"memory": {"content": self.l2}}

            def write_scenario(self, _path, content, summary=None):
                self.l2 = content
                return {"content": content, "summary": summary}

            def read_scenario(self, _path):
                return {"content": self.l2}

            def read_core(self):
                return {"content": self.l3}

            def write_core(self, content):
                self.l3 = content
                return {"content": content}

        native = FakeNative()
        with patch("core.native_memory_client", return_value=native):
            first = core.promote_l3("memorycore:L2:scenario.md")
            first_length = len(native.l3)
            second = core.promote_l3("memorycore:L2:scenario.md")

        self.assertTrue(first["verified"])
        self.assertTrue(second["verified"])
        self.assertEqual(native.l3.count("source_l2: scenario.md"), 1)
        self.assertEqual(len(native.l3), first_length)

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
        commit = subprocess.check_output(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], text=True, encoding="utf-8").strip()
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


class TeamMemoryRetentionTest(unittest.TestCase):
    """Retention and compaction for memory_revisions."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "retention.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def _populate(self, store, count: int = 3):
        """Publish a memory and create ``count`` revisions by repeated activation/export-import."""
        pub = store.publish({
            "type": "decision",
            "title": "Retention decision",
            "content": "Should survive compaction.",
            "status": "candidate",
            "author_agent": "agent-a",
        }, default_status="candidate")
        mid = pub["memory"]["id"]
        # Activate to bump the revision, then export-import to clone revisions
        store.activate(mid, reviewer="reviewer-a")
        for _ in range(count - 2):
            bundle = store.export_bundle()
            clone = TeamMemoryStore(Path(self.temp.name) / f"clone-{id(store)}-{_}.sqlite3")
            clone.import_bundle(bundle)
            clone.activate(mid, reviewer="reviewer-a")
            store.import_bundle(clone.export_bundle())
        return mid

    def test_compact_keep_1_preserves_current_revision(self):
        store = TeamMemoryStore(self.db)
        mid = self._populate(store, count=4)
        before = store.health()["retention"][mid]
        self.assertGreater(before["total_revisions"], 1)
        result = store.compact(keep=1)
        self.assertGreaterEqual(result["purged"], 0)
        after = store.health()["retention"][mid]
        self.assertGreaterEqual(after["total_revisions"], 1)
        # current record is still there
        self.assertEqual(store.get(mid)["result"]["id"], mid)

    def test_compact_protects_ancestor_chain(self):
        store = TeamMemoryStore(self.db)
        mid = self._populate(store, count=5)
        before_chain = store.health()["retention"][mid]["current_ancestor_chain"]
        store.compact(keep=100)
        after = store.health()["retention"][mid]
        # ancestor chain is never shortened by compaction
        self.assertGreaterEqual(after["current_ancestor_chain"], 1)
        self.assertEqual(store.get(mid)["result"]["id"], mid)

    def test_compact_keep_2_preserves_extra_unprotected(self):
        store = TeamMemoryStore(self.db)
        mid = self._populate(store, count=4)
        before = store.health()["retention"][mid]["total_revisions"]
        store.compact(keep=2)
        after = store.health()["retention"][mid]["total_revisions"]
        # keep=2 should preserve at least 2 unprotected + ancestors
        self.assertGreaterEqual(after, 2)
        self.assertLessEqual(after, before)

    def test_compact_rejects_keep_0(self):
        store = TeamMemoryStore(self.db)
        with self.assertRaises(ValueError):
            store.compact(keep=0)

    def test_health_reports_retention_diagnostics(self):
        store = TeamMemoryStore(self.db)
        mid = self._populate(store, count=3)
        health = store.health()
        self.assertIn("retention", health)
        self.assertIn(mid, health["retention"])
        entry = health["retention"][mid]
        self.assertIn("total_revisions", entry)
        self.assertIn("current_ancestor_chain", entry)
        self.assertGreaterEqual(entry["total_revisions"], 1)
        self.assertGreaterEqual(entry["current_ancestor_chain"], 1)

    def test_import_reports_conflict_for_missing_ancestor(self):
        """Import a record whose parent_revision does not exist locally."""
        store = TeamMemoryStore(self.db)
        bundle = {
            "kind": "repository-memory-team-bundle",
            "schema_version": 3,
            "records": [
                {
                    "id": "team:decision:orphan",
                    "memory_type": "decision",
                    "title": "Orphan memory",
                    "content": "Parent revision was compacted away on source.",
                    "summary": "orphan test",
                    "scope": "{}",
                    "provenance": "{}",
                    "confidence": 0.5,
                    "status": "active",
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                    "revision": 5,
                    "origin_node": "source-node",
                    "parent_revision": "source-node:4",
                }
            ],
            "revisions": [],
            "feedback": [],
        }
        result = store.import_bundle(bundle)
        self.assertEqual(result["imported"]["inserted"], 0)
        self.assertEqual(result["imported"]["conflicts"], 1)
        self.assertIn("parent_revision not found locally", result["imported"]["conflict_records"][0]["reason"])


class GatewayCredentialSourceTest(unittest.TestCase):
    """Where the endpoint credential may come from, and where it may not go.

    An agent host launched from a GUI inherits no shell environment, so a
    credential named by environment variable is unreachable there and the remote
    provider silently stops being used.  Pointing at the file that already holds
    the secret fixes that without this package ever storing it.
    """

    SECRET = "gateway-file-test-key"

    def setUp(self):
        import local_embedding

        self.local_embedding = local_embedding
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self._environ = dict(os.environ)
        for name in ("REPOSITORY_MEMORY_SEMANTIC_API_KEY", "GATEWAY_KEY_FOR_TEST"):
            os.environ.pop(name, None)
        os.environ.update(
            {
                "XDG_CACHE_HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root),
                "XDG_DATA_HOME": str(self.root),
            }
        )
        self.addCleanup(self.reset_environment)
        self.addCleanup(self.directory.cleanup)

    def reset_environment(self):
        os.environ.clear()
        os.environ.update(self._environ)

    def write_config(self, semantic: dict):
        path = self.root / "repository-memory" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"semantic": semantic}), encoding="utf-8")
        return path

    def test_json_path_reads_a_credential_owned_by_another_tool(self):
        source = self.root / "other-tool.json"
        source.write_text(
            json.dumps({"models": {"providers": {"house": {"apiKey": self.SECRET}}}}),
            encoding="utf-8",
        )
        self.write_config(
            {
                "provider": "gateway",
                "api_key_file": str(source),
                "api_key_json_path": "models.providers.house.apiKey",
            }
        )
        self.assertEqual(self.local_embedding._gateway_api_key(), self.SECRET)

    def test_plain_file_is_the_credential_and_trailing_newline_is_not(self):
        source = self.root / "token"
        source.write_text(f"{self.SECRET}\n", encoding="utf-8")
        self.write_config({"provider": "gateway", "api_key_file": str(source)})
        self.assertEqual(self.local_embedding._gateway_api_key(), self.SECRET)

    def test_environment_still_wins_over_the_file(self):
        source = self.root / "token"
        source.write_text("stale-value-from-disk", encoding="utf-8")
        self.write_config({"provider": "gateway", "api_key_file": str(source)})
        os.environ["REPOSITORY_MEMORY_SEMANTIC_API_KEY"] = self.SECRET
        self.assertEqual(self.local_embedding._gateway_api_key(), self.SECRET)

    def test_named_environment_variable_still_wins_over_the_file(self):
        source = self.root / "token"
        source.write_text("stale-value-from-disk", encoding="utf-8")
        self.write_config(
            {"provider": "gateway", "api_key_env": "GATEWAY_KEY_FOR_TEST", "api_key_file": str(source)}
        )
        os.environ["GATEWAY_KEY_FOR_TEST"] = self.SECRET
        self.assertEqual(self.local_embedding._gateway_api_key(), self.SECRET)

    def test_every_unreadable_shape_degrades_to_no_credential(self):
        missing = self.root / "does-not-exist.json"
        self.write_config({"provider": "gateway", "api_key_file": str(missing)})
        self.assertEqual(self.local_embedding._gateway_api_key(), "")

        not_json = self.root / "not-json"
        not_json.write_text("<html>login page</html>", encoding="utf-8")
        self.write_config(
            {"provider": "gateway", "api_key_file": str(not_json), "api_key_json_path": "a.b"}
        )
        self.assertEqual(self.local_embedding._gateway_api_key(), "")

        wrong_path = self.root / "other.json"
        wrong_path.write_text(json.dumps({"models": {}}), encoding="utf-8")
        self.write_config(
            {"provider": "gateway", "api_key_file": str(wrong_path), "api_key_json_path": "models.providers.house.apiKey"}
        )
        self.assertEqual(self.local_embedding._gateway_api_key(), "")

        # A non-string leaf is a configuration error, not a credential.
        numeric = self.root / "numeric.json"
        numeric.write_text(json.dumps({"key": 1234}), encoding="utf-8")
        self.write_config({"provider": "gateway", "api_key_file": str(numeric), "api_key_json_path": "key"})
        self.assertEqual(self.local_embedding._gateway_api_key(), "")

    def test_configure_records_the_location_and_never_the_secret(self):
        import semantic_repository

        source = self.root / "other-tool.json"
        source.write_text(json.dumps({"k": self.SECRET}), encoding="utf-8")
        semantic_repository.configure(
            model="text-embedding-v3",
            provider="gateway",
            endpoint="https://endpoint.invalid/v1",
            api_key_file=str(source),
            api_key_json_path="k",
        )
        written = (self.root / "repository-memory" / "config.json").read_text(encoding="utf-8")
        self.assertNotIn(self.SECRET, written)
        self.assertIn("api_key_file", written)
        self.assertEqual(self.local_embedding._gateway_api_key(), self.SECRET)

    def test_status_reports_presence_without_the_value(self):
        source = self.root / "token"
        source.write_text(self.SECRET, encoding="utf-8")
        self.write_config(
            {
                "enabled": True,
                "provider": "gateway",
                "endpoint": "https://endpoint.invalid/v1",
                "api_key_file": str(source),
            }
        )
        status = self.local_embedding.embedding_status(probe=False)
        self.assertNotIn(self.SECRET, json.dumps(status))
        self.assertIs(status.get("api_key_present"), True)


class GatewayEmbeddingTest(unittest.TestCase):
    """The optional remote encoder, exercised without a network."""

    # Deliberately not shaped like a real credential: the tree scanner treats
    # an "sk-" blob as a leak wherever it appears, and it is right to.
    SECRET = "gateway-unit-test-key"

    def setUp(self):
        import local_embedding

        self.local_embedding = local_embedding
        self.directory = tempfile.TemporaryDirectory()
        self.calls: list[dict] = []
        self._environ = dict(os.environ)
        os.environ.update(
            {
                "XDG_CACHE_HOME": self.directory.name,
                "XDG_CONFIG_HOME": self.directory.name,
                "XDG_DATA_HOME": self.directory.name,
                "REPOSITORY_MEMORY_SEMANTIC_ENABLED": "1",
                "REPOSITORY_MEMORY_SEMANTIC_PROVIDER": "gateway",
                "REPOSITORY_MEMORY_SEMANTIC_ENDPOINT": "https://endpoint.invalid/v1",
                "REPOSITORY_MEMORY_SEMANTIC_API_KEY": self.SECRET,
            }
        )
        self.addCleanup(self.reset_environment)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.reset_probe)
        self.reset_probe()

    def reset_environment(self):
        os.environ.clear()
        os.environ.update(self._environ)

    def reset_probe(self):
        self.local_embedding._GATEWAY_PROBE = None
        self.local_embedding._GATEWAY_PROBE_KEY = None

    def endpoint(self, responder):
        return patch.object(self.local_embedding.urllib.request, "urlopen", responder)

    def responder(self, *, width: int | None = None, reverse: bool = False):
        """Answer like an OpenAI-compatible endpoint, recording each request."""

        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode("utf-8")

            def read(self):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def handler(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            self.calls.append({"body": body, "headers": dict(request.headers), "timeout": timeout})
            size = width or int(body.get("dimensions") or 1024)
            rows = [
                {"index": position, "embedding": [float((position + 1) * (offset + 1) % 7) for offset in range(size)]}
                for position in range(len(body["input"]))
            ]
            return Response({"data": list(reversed(rows)) if reverse else rows})

        return handler

    def test_status_reports_verified_endpoint_without_the_credential(self):
        with self.endpoint(self.responder()):
            status = self.local_embedding.embedding_status(probe=True)
        self.assertTrue(status["available"])
        self.assertEqual(status["provider"], "gateway")
        self.assertEqual(status["dimension"], 512)
        self.assertTrue(status["api_key_present"])
        self.assertNotIn(self.SECRET, json.dumps(status))
        self.assertLessEqual(self.calls[0]["timeout"], self.local_embedding.GATEWAY_PROBE_TIMEOUT)

    def test_unverified_endpoint_is_not_reported_as_available(self):
        status = self.local_embedding.embedding_status(probe=False)
        self.assertFalse(status["available"])
        self.assertFalse(status["verified"])
        self.assertEqual(status["strategy"], "lexical-fallback")
        self.assertEqual(self.calls, [])

    def test_vectors_are_normalized_and_reordered_by_index(self):
        with self.endpoint(self.responder(reverse=True)):
            vectors, spec = self.local_embedding.encode_documents(["first", "second", "third"])
        self.assertEqual(spec["provider"], "gateway")
        self.assertEqual([len(vector) for vector in vectors], [512, 512, 512])
        for vector in vectors:
            self.assertAlmostEqual(sum(value * value for value in vector) ** 0.5, 1.0, places=6)
        # A reversed response must not silently pair document 0 with vector N.
        self.assertNotEqual(vectors[0][0], vectors[1][0])

    def test_batches_respect_the_configured_limit(self):
        with self.endpoint(self.responder()):
            self.local_embedding.embedding_status(probe=True)
            self.calls.clear()
            self.local_embedding.encode_documents([f"document {index}" for index in range(25)])
        self.assertEqual([len(call["body"]["input"]) for call in self.calls], [10, 10, 5])

    def test_failure_falls_back_wholesale_and_scrubs_the_credential(self):
        def failing(request, timeout=None):
            raise RuntimeError(f"HTTP 401 Unauthorized for {self.SECRET}")

        with self.endpoint(failing):
            vectors, spec = self.local_embedding.encode_documents(["alpha", "beta"])
            status = self.local_embedding.embedding_status(probe=True)
        # A half-remote batch cannot be described by one provider/dimension
        # triple, so the whole batch has to come from the same encoder.
        self.assertEqual(spec["provider"], "builtin")
        self.assertEqual([len(vector) for vector in vectors], [384, 384])
        self.assertFalse(status["available"])
        self.assertNotIn(self.SECRET, json.dumps(status))
        self.assertIn("***", status["error"])

    def test_a_cached_failure_keeps_queries_off_the_network(self):
        def failing(request, timeout=None):
            raise RuntimeError("connection refused")

        with self.endpoint(failing):
            self.local_embedding.embedding_status(probe=True)
        self.reset_probe()

        def forbidden(request, timeout=None):
            raise AssertionError("a cached failure must not be retried per query")

        with self.endpoint(forbidden):
            vector = self.local_embedding.vectorize("李宁最近在做什么")
        self.assertEqual(len(vector), self.local_embedding.EMBEDDING_DIMENSION)

    def test_repeated_failures_back_off(self):
        def failing(request, timeout=None):
            raise RuntimeError("connection refused")

        config = self.local_embedding._gateway_config()
        key = self.local_embedding._probe_cache_key(config)
        path = self.local_embedding._probe_cache_path()
        with self.endpoint(failing):
            for attempt in range(1, 4):
                self.reset_probe()
                self.local_embedding.embedding_status(probe=True)
                cached = self.local_embedding._read_probe_raw(key)
                self.assertEqual(cached["failures"], attempt)
                # Within its window the cached failure is reused, so age it to
                # reach the next probe; that is exactly what makes a dead
                # endpoint cost one timeout per window instead of one per call.
                self.assertIsNotNone(self.local_embedding._read_probe_cache(key))
                path.write_text(json.dumps({**cached, "checked_at": cached["checked_at"] - 10_000}), encoding="utf-8")
        aged = self.local_embedding._read_probe_raw(key)
        self.assertEqual(aged["failures"], 3)
        # Three failures widen the window past the one-minute floor: an entry
        # older than the base TTL is still authoritative, so the endpoint is
        # not re-probed once a minute forever.
        path.write_text(
            json.dumps({**aged, "checked_at": time.time() - (self.local_embedding.GATEWAY_PROBE_TTL_ERROR + 30)}),
            encoding="utf-8",
        )
        self.assertIsNotNone(self.local_embedding._read_probe_cache(key))
        path.write_text(
            json.dumps({**aged, "checked_at": time.time() - (self.local_embedding.GATEWAY_PROBE_TTL_MAX + 30)}),
            encoding="utf-8",
        )
        self.assertIsNone(self.local_embedding._read_probe_cache(key))

    def test_a_disabled_gateway_leaves_the_default_provider_untouched(self):
        os.environ["REPOSITORY_MEMORY_SEMANTIC_ENABLED"] = "0"
        self.reset_probe()

        def forbidden(request, timeout=None):
            raise AssertionError("the default install must never call an endpoint")

        with self.endpoint(forbidden):
            status = self.local_embedding.embedding_status(probe=True)
            vectors, spec = self.local_embedding.encode_documents(["alpha"])
        self.assertEqual(status["provider"], self.local_embedding.EMBEDDING_PROVIDER)
        self.assertEqual(status["configured_by"], "default")
        self.assertTrue(status["available"])
        self.assertEqual(spec["provider"], "builtin")
        self.assertEqual(len(vectors[0]), self.local_embedding.EMBEDDING_DIMENSION)

    def test_a_stale_local_model_name_is_not_sent_to_the_endpoint(self):
        os.environ["REPOSITORY_MEMORY_SEMANTIC_MODEL"] = self.local_embedding.EMBEDDING_MODEL
        self.assertEqual(self.local_embedding._gateway_config()["model"], self.local_embedding.GATEWAY_DEFAULT_MODEL)

    def test_a_corpus_is_encoded_into_a_packed_buffer(self):
        import array

        with self.endpoint(self.responder()):
            buffer, width, spec = self.local_embedding.encode_document_vectors([f"document {index}" for index in range(12)])
        # A list of lists costs roughly eight bytes of Python object overhead
        # per float; at corpus scale that difference is the whole budget.
        self.assertIsInstance(buffer, array.array)
        self.assertEqual(buffer.itemsize, 4)
        self.assertEqual(width, 512)
        self.assertEqual(len(buffer), 12 * 512)
        self.assertEqual(spec["provider"], "gateway")

    def test_the_builtin_corpus_path_uses_the_same_packed_buffer(self):
        import array

        os.environ["REPOSITORY_MEMORY_SEMANTIC_ENABLED"] = "0"
        buffer, width, spec = self.local_embedding.encode_document_vectors(["alpha", "beta", "gamma"])
        self.assertIsInstance(buffer, array.array)
        self.assertEqual(width, self.local_embedding.EMBEDDING_DIMENSION)
        self.assertEqual(len(buffer), 3 * self.local_embedding.EMBEDDING_DIMENSION)
        self.assertEqual(spec["provider"], "builtin")


class CarvedTermProvenanceTest(unittest.TestCase):
    """A segmenter's words are claims; only the joins around them are guesses."""

    def setUp(self) -> None:
        import fallback
        import tokenize_query

        self.fallback = fallback
        self.tokenize_query = tokenize_query

    def test_segmented_words_are_not_carved(self) -> None:
        status = self.tokenize_query.tokenizer_status()
        if status.get("name") != "jieba":
            self.skipTest("requires the jieba extra")
        terms = self.tokenize_query.query_terms("腌制泡菜的传统做法")
        carved = self.tokenize_query.carved_query_terms("腌制泡菜的传统做法") & set(terms)
        # The joins this module manufactured are carved ...
        self.assertIn("腌制泡菜", carved)
        # ... and the words the segmenter returned are not.
        self.assertNotIn("腌制", carved)
        self.assertNotIn("泡菜", carved)

    def test_absent_topic_is_not_answered_by_a_surviving_generic_phrase(self) -> None:
        status = self.tokenize_query.tokenizer_status()
        if status.get("name") != "jieba":
            self.skipTest("requires the jieba extra")
        query = "腌制泡菜的传统做法"
        terms = self.tokenize_query.query_terms(query)
        carved = self.tokenize_query.carved_query_terms(query) & set(terms)
        real = frozenset(set(terms) - carved)
        # The corpus writes "传统做法" all over its prose and has never heard of
        # pickling.  Dropping the unreachable join must not leave the generic
        # phrase alone in the requirement.
        unreachable = frozenset({"腌制泡菜"})
        support = self.fallback._claim_support(
            terms,
            "传统做法是把所有 patch 保留，这里讨论 RLVR training pipeline。",
            1,
            5,
            unreachable=unreachable,
            real_terms=real,
            path="survey/section4-training-pipeline.md",
        )
        self.assertNotEqual(support["claim_support"], "direct")
        self.assertIn("腌制", support["unmatched_terms"])
        self.assertIn("泡菜", support["unmatched_terms"])

    def test_builtin_path_keeps_collapse_then_exclude(self) -> None:
        # With no segmenter every fragment is a guess, so nothing is restored
        # and the measured builtin behaviour is unchanged: the carved fragment
        # the corpus never contained drops out and the terms the user actually
        # delimited carry the claim.
        support = self.fallback._claim_support(
            ["octo-daemon", "健康监控", "cron", "是怎么配置", "是怎么"],
            "octo-daemon 的健康监控 cron 每天跑一次。",
            1,
            5,
            unreachable=frozenset({"是怎么配置", "是怎么"}),
            real_terms=frozenset(),
            path="standup/卫海天.md",
        )
        self.assertEqual(support["claim_support"], "direct")
        self.assertNotIn("是怎么配置", support["matched_terms"])

    def test_all_absent_carved_query_still_abstains(self) -> None:
        # Excluding must never empty the requirement into the ``direct`` branch.
        support = self.fallback._claim_support(
            ["是怎么配置", "是怎么", "怎么配", "么配置"],
            "octo-daemon 的健康监控 cron 每天跑一次。",
            1,
            5,
            unreachable=frozenset({"是怎么配置"}),
            real_terms=frozenset(),
            path="standup/卫海天.md",
        )
        self.assertNotEqual(support["claim_support"], "direct")

    def test_reachable_join_stays_required(self) -> None:
        support = self.fallback._claim_support(
            ["火山云", "火山", "部署"],
            "我们在别的云上部署了火山相关的服务。",
            1,
            5,
            unreachable=frozenset(),
            real_terms=frozenset({"火山", "部署"}),
            path="notes/x.md",
        )
        self.assertIn("火山云", support["unmatched_terms"])
        self.assertNotEqual(support["claim_support"], "direct")


class SemanticDeferralTest(unittest.TestCase):
    """Deferring a large source must postpone the build, not the cache read."""

    def setUp(self) -> None:
        import semantic_repository

        self.semantic_repository = semantic_repository

    def _view(self, root: Path):
        from models import SourceSpec, SourceView

        spec = SourceSpec(id="deferral", root=root, repository="deferral")
        return SourceView(
            spec=spec,
            path=root,
            commit="c" * 40,
            branch="main",
            commit_type="local",
            dirty=False,
            metadata={},
        )

    def test_missing_cache_defers_without_probing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            view = self._view(Path(directory))
            with patch.object(self.semantic_repository, "embedding_status") as status:
                result = self.semantic_repository.ensure(view, {"documents": []}, build=False)
            # No cache for this commit: answer before paying for readiness.
            status.assert_not_called()
            self.assertTrue(result["deferred"])
            self.assertFalse(result["available"])
            self.assertEqual(result["defer_reason"], "large_repository_first_pass")

    def test_signature_mismatch_defers_rather_than_scoring_two_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            view = self._view(Path(directory))
            meta = self.semantic_repository._meta_path(view, False)
            vectors = self.semantic_repository._vectors_path(view, False)
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps(
                    {
                        "schema_version": self.semantic_repository.SCHEMA_VERSION,
                        "commit": view.commit,
                        "provider": "gateway",
                        "model": "text-embedding-v3",
                        "dimension": 512,
                        "paths": ["a.md"],
                    }
                ),
                encoding="utf-8",
            )
            vectors.write_bytes(b"\x00" * (512 * 4))
            with patch.object(
                self.semantic_repository,
                "embedding_status",
                return_value={
                    "configured": True,
                    "available": True,
                    "provider": "builtin",
                    "model": "builtin-char-ngram-v1",
                    "dimension": 384,
                },
            ):
                result = self.semantic_repository.ensure(view, {"documents": []}, build=False)
        # The cached vectors describe a different embedding space than the one
        # ``vectorize`` would encode the query in, so they must not be scored.
        self.assertTrue(result["deferred"])
        self.assertEqual(result["defer_reason"], "semantic_cache_signature_mismatch")


if __name__ == "__main__":
    unittest.main()
