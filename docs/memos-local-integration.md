# MemOS Local integration

`repository-memory` can install MemOS Local as the local conversation-memory
plane while keeping Git-backed repository evidence as a separate, traceable
plane.

```text
OpenClaw
├── memory_search / memory_get / memory_timeline
│   └── MemOS Local: L0/L1 conversation memory, task summaries, skills, viewer
└── repository-memory__memory_search / repository-memory__memory_get
    └── repository-memory: Git source, commit/path/line citation, freshness
```

This is a real plugin installation boundary, not a Python reimplementation of
the upstream package. The installer copies the selected upstream revision to a
user data staging directory, builds there, and asks OpenClaw to install the
staged plugin. The upstream checkout is never changed.

## One-command local setup

From a MemOS checkout, run:

```bash
export MEMOS_SOURCE_ROOT=/path/to/MemOS
repository-memory memos doctor --json
repository-memory memos install --json
```

The installer verifies the plugin package and Git revision, stages a copy,
builds it, links it with OpenClaw, selects the MemOS memory slot, disables the
built-in OpenClaw memory search to prevent duplicate recall, preserves the
repository-memory MCP, and creates a config backup.

The current upstream package has a CommonJS/ESM build mismatch and does not
copy its bundled Skill during TypeScript compilation. The installer applies a
small compatibility fix only in the generated staging copy. It never edits
the discovered upstream checkout.

## Commands

```bash
repository-memory memos doctor --json
repository-memory memos configure --source /path/to/MemOS --json
repository-memory memos install --source /path/to/MemOS --json
repository-memory memos disable --json
```

`disable` restores the OpenClaw memory slot to `memory-core` and disables only
the MemOS plugin entry. It does not delete the MemOS database, staged source,
or existing memory assets.

## What is and is not unified

The host is unified; provenance is not flattened:

- Use `repository-memory__memory_search` for project facts and Git citations.
- Use native `memory_search` for conversation memory and task history.
- Conversation memory is not a Git fact unless it contains a linked repository
  citation that the repository lane validates.
- No cross-backend score fusion is performed.

Embedding and summarizer settings remain user-level OpenClaw configuration.
If they are unavailable, MemOS's local fallback may still work; the
repository-memory doctor never labels that as a neural semantic provider.

## Verification

```bash
openclaw gateway restart --safe --json
openclaw plugins inspect memos-local-openclaw-plugin --runtime --json
repository-memory memos doctor --json
```

Success requires `status=loaded`, `memorySlotSelected=true`, a nonzero
`hookCount`, and a running gateway. A package directory or endpoint alone is
not evidence that the memory pipeline is loaded or populated.

The MemOS plugin may start its local SQLite-backed Viewer when the gateway
loads it. No standalone daemon or remote model is started by this installer.
