# TencentDB component integration

Repository Memory carries a clean source-only snapshot of the reusable
TencentDB Agent Memory modules. The exact upstream revision and imported paths
are in:

```text
skills/repository-memory/vendor/tencentdb-agent-memory-reference/MANIFEST.json
```

The local TencentDB checkout was dirty during the import. The snapshot came
from `git archive HEAD`, so local experiments, semantic Wiki changes, runtime
databases, `node_modules`, and credentials were not copied.

## What is actually used

| Upstream capability | Repository Memory use |
| --- | --- |
| L0 conversation recorder | bounded, redacted `capture-turn` payloads with idempotent write/read-back |
| L1 atomic extraction | native v3 observation; `pending` stays pending until records are observed |
| L2 scene extractor/navigation | native scenario listing/read and accepted-vs-candidate lifecycle |
| L3 persona/profile | explicit promotion and `core/read` verification only |
| OpenClaw recall hook | `before_prompt_build` invokes the shared CLI with `scope=memory` |
| OpenClaw capture hook | `agent_end` invokes the shared CLI with `capture-turn` |
| MemoryKnowledge Wiki/code modules | pinned adapter reference; repository-memory keeps citation-first local indexing and does not black-box fuse scores |

The TypeScript snapshot is also the default source for the local native service
after installation. It is kept separate from the Python CLI/MCP process: the
Python runtime remains the public boundary, while the launchd-managed service
runs the copied MemoryCore source with user-level dependencies and state. A
user-configured compatible checkout can still override the bundled source, but
doctor must report that override explicitly.

## Runtime checks

Run:

```bash
repository-memory doctor --json
```

The response contains `upstream_components` with the pinned commit, file count,
component roles, and the explicit fact that dirty upstream worktree changes
were excluded. The same field is emitted after installation because `vendor/`
is part of the Skill package.

`retrieval_mode=keyword-only` remains honest when no embedding provider is
configured. The copied modules do not turn lexical search into semantic search.
