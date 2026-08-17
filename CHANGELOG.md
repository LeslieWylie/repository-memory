# Changelog

## 0.7.7

- Port the useful MemOS Local lifecycle mechanics into the independent
  provider-free runtime: episode/turn identifiers, conservative turn relation
  classification, feedback-weighted trace values, and time-decayed priority.
- Add an evidence-backed L2 policy candidate pool requiring multiple distinct
  episodes and retaining source record IDs.
- Keep Git citation retrieval, the CLI/MCP contract, and the canonical source
  independent from the MemOS Node package.

## 0.7.5

- Preserve explicit OpenClaw turn boundaries during automatic capture: use the
  host's position/timestamp cursor and original user text when available, so
  recalled context and old session messages do not become new memory.
- Include relative paths in the disposable large-repository FTS stream. Short
  CJK/person-name queries can now reach filename-anchored evidence instead of
  being discarded before deterministic ranking.

## 0.7.4

- Add conservative date anchors and explicit local-reference metadata to the
  disposable repository index.
- Improve latest/report routing from headings and explicit date fields without
  treating arbitrary body dates as document dates.
- Add explainable one-hop relationship expansion and `related` citations for
  explicit local links, without a graph service or opaque score fusion.
- Preserve the zero-service, citation-first default and report the builtin
  projection as non-neural.

## 0.7.3

- Align the AML wrapper with the current public contract: accept any non-empty
  message role and normalize Unix-millisecond source timestamps.
- Use source event time in `created_at` and add a bounded recency signal for
  explicit latest/recent queries.
- Keep the public Add response to the exact declared fields while retaining
  internal write/read-back verification.
- Add a submission-ready code-route runbook without claiming local fixture
  scores as leaderboard results.

## 0.7.1

- make `repository-memory benchmark --suite public --json` discover the
  checked-out public repository root automatically;
- keep explicit `--root` and manifest-root behavior unchanged.

## 0.7.0

- add a dependency-free synchronous Agent Memory Leaderboard Add/Search
  wrapper with user isolation, auth, health and Docker submission instructions;
- expose `repository-memory-aml` as a packaged entry point;
- keep AML ingestion on the standalone L0/L1 path without changing the
  citation-first repository search contract.

## 0.6.0

- Added explicit supervisor receipts and safe candidate review for Team Memory
  and standalone L2 scenarios.
- Added a reproducible `benchmark` command for the bundled fixture and user-
  supplied external benchmark manifests.
- Added the provider protocol manifest/normalization seam without adding a
  runtime dependency on any external memory product.

## 0.3.0

- Bundle a clean, pinned TencentDB Agent Memory source snapshot for the native
  L0-L3 lifecycle and MemoryKnowledge adapter reference.
- Add shared-runtime OpenClaw `before_prompt_build` memory recall with labelled
  layer/status context and no candidate injection.
- Align post-turn capture with upstream sanitization so injected recall and
  assistant code blocks do not feed back into durable memory.
- Remove stale legacy memory tool names from the selected OpenClaw agent's
  active allowlist while keeping the old plugin entries disabled for rollback.

All notable changes to this project are recorded here. The project and its
bundled runtime currently use the same release version. The MCP protocol
revision is a separate compatibility identifier; see
[`docs/compatibility.md`](docs/compatibility.md).

## [Unreleased]

- Keep unreleased changes here until a tagged release is prepared.
- Simplify the public Skill instructions and UI metadata around the real
  doctor -> search -> get workflow, with explicit repository/memory scopes,
  citation handling, and a non-blocking development-tool boundary.
- Group GitHub Actions and Python Dependabot updates so routine maintenance
  opens at most one pull request per ecosystem.
- Add shared Team Memory with explicit publish, context hydration, feedback,
  supersede lifecycle, and reusable decision/failure/discovery/solution/handoff
  records.
- Add a replaceable `TeamMemoryBackend` seam, SQLite WAL/busy-timeout/retry
  behavior, validity-window filtering, stale/wrong lifecycle transitions, and
  explicit portable Team Memory export/import bundles.
- Rename context retrieval from `hybrid-lexical` to the accurate
  `multi-source-lexical`; repository and Team Memory recall run in parallel but
  keep scores and provenance separate.
- Add causal Team Memory revisions (`revision`, `origin_node`,
  `parent_revision`), conflict-aware bundle merge, automatic migration for
  older SQLite databases, and explicit candidate activation after review.
- Extend Team Memory bundles with an append-only revision log for skipped-version
  fast-forward, separate activation reviewer metadata from authorship, and add
  stable feedback IDs for cross-machine deduplication.
- Change the OpenClaw guard to advisory/output-audit behavior; it no longer
  blocks normal file, shell, Git, test, or debugging tools.
- Strengthen evaluator qrels validation, citation commit pinning, and
  multi-gold Recall@5 accounting.
- Add a standard wheel/console entry point and a Windows `msvcrt` snapshot
  lock fallback.
- Separate each doctor memory layer's adapter capability, API readiness, data
  population, and read-back state so supported/reachable layers are never
  reported as populated without records from the layer response.

## [0.2.0] - 2026-08-11

- Added cross-platform GitHub Actions coverage for Python 3.10, 3.12, and
  3.13 on Ubuntu, macOS, and Windows.
- Added a public citation/P@1 regression set with negative-query abstention
  and a deterministic CI gate.
- Unified the project, Skill runtime, installer client, and OpenClaw plugin
  release version at `0.2.0`.
- Added security, contributing, dependency-update, and compatibility
  documentation.
- Kept OpenClaw guard enforcement explicit: audit is the default, enforce is
  opt-in.

## [0.1.0]

- Initial citation-first repository memory runtime, CLI, stdio MCP server,
  optional MemoryCore adapter, and OpenClaw integration.
## 0.7.6

- Add an isolated semantic benchmark A/B selector with truthful effective-mode diagnostics.
- Add a zero-dependency read-only local dashboard (`gui --serve`).
- Record repository index scale metadata and reuse it during large-source routing.
- Add `memory evolve` for explicit L2 projection plus optional supervisor review; L3 remains explicit.
