# Changelog

All notable changes to this project are recorded here. The project and its
bundled runtime currently use the same release version. The MCP protocol
revision is a separate compatibility identifier; see
[`docs/compatibility.md`](docs/compatibility.md).

## [Unreleased]

- Keep unreleased changes here until a tagged release is prepared.
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
