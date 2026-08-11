# Changelog

All notable changes to this project are recorded here. The project and its
bundled runtime currently use the same release version. The MCP protocol
revision is a separate compatibility identifier; see
[`docs/compatibility.md`](docs/compatibility.md).

## [Unreleased]

- Keep unreleased changes here until a tagged release is prepared.
- Narrow host guard enforcement to explicit file reads, source-reading
  commands, and destructive commands; generic execution and maintenance tools
  remain available.
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
