# Compatibility and version policy

There are two different version numbers in this project:

| Surface | Current value | Meaning |
| --- | --- | --- |
| Repository Memory release | `0.3.1` | The Python project, vendored TencentDB component snapshot, shared runtime, installer, and OpenClaw plugin release. Vendor-backed MemoryCore launch and idempotent L2/L3 promotion are included. |
| MCP protocol | `2026-07-28` | The wire-level protocol revision negotiated with a host. |

The release version is stored in
`skills/repository-memory/VERSION`; runtime components read that file when
they report their version. `pyproject.toml` and the plugin manifest repeat the
same value because packaging and JSON manifests require static metadata. CI
should fail a release review if those values diverge.

The Python distribution is a standard wheel with a
`repository-memory` console entry point. The legacy Skill directory remains
the source layout used by the installer, so existing OpenClaw/Codex/Claude
installations continue to use the same runtime files.

The MCP server advertises `2026-07-28` first. It retains compatibility with
the older initialize/Content-Length protocol revisions listed by
`mcp_server.py`; compatibility is a migration path, not a second retrieval
implementation. New clients should use modern discovery and per-request
protocol metadata.

Versioning rules:

- Patch releases fix behavior without changing the public JSON contract.
- Minor releases may add fields, tools, or adapters while preserving existing
  fields and commands.
- Major releases may remove compatibility behavior or change verification
  semantics and require a migration note.
- Index schema changes must invalidate or migrate derived caches; they must
  never silently reinterpret an old index as current.
- A release must run the Python tests, OpenClaw contract tests, JSON checks,
  and public retrieval gate on the supported OS/Python matrix.
