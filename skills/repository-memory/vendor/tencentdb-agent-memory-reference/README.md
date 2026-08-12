# Vendored TencentDB Agent Memory components

This directory is a clean, source-only snapshot of reusable upstream modules.
It was imported from the upstream Git commit recorded in `MANIFEST.json` with
`git archive HEAD`; uncommitted changes from the local TencentDB checkout are
intentionally excluded.

The snapshot is not a second runtime or a second source of truth. The active
Repository Memory runtime still owns the public CLI, MCP result contract,
repository citations, and user-level configuration. The vendored modules are
used in two ways:

1. the native adapter follows the upstream v3 boundaries for L0/L1/L2/L3;
2. the OpenClaw extension follows the upstream lifecycle pattern: recall before
   prompt construction and bounded capture after a completed turn.

`MemoryCore` remains dependency-bearing TypeScript code. It is kept here so a
host can build the native service from a pinned source snapshot when the
upstream Node dependencies are installed. `MemoryKnowledge` is included as a
clean reference for Wiki/code-graph adapters; it is not silently enabled as a
second ranking backend.

Do not add `node_modules`, `dist`, local SQLite data, credentials, or dirty
working-tree patches to this directory.
