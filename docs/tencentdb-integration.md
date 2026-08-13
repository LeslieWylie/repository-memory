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
| L1 atomic extraction | compatibility observation; `pending` stays pending until records are observed |
| L2 scene extractor/navigation | native scenario listing/read and accepted-vs-candidate lifecycle |
| L3 persona/profile | explicit promotion and `core/read` verification only |
| OpenClaw recall hook | `before_prompt_build` invokes the shared CLI with `scope=memory` |
| OpenClaw capture hook | `agent_end` invokes the shared CLI with `capture-turn` |
| MemoryKnowledge Wiki/code modules | optional user-level Wiki/CodeGraph service; repository-memory keeps citation-first local indexing and does not black-box fuse scores |

The TypeScript snapshot is reference material and optional compatibility code,
not the default runtime. The Python CLI/MCP process is self-contained and uses
the built-in standalone backend; a user-configured compatible checkout can
still be enabled separately, but doctor must report that override explicitly.

## MemoryKnowledge boundary

The upstream source separates content knowledge from the MemoryCore metadata
plane. `repository-memory knowledge` manages the optional local MemoryKnowledge
service and exposes explicit operations:

```text
knowledge status
knowledge create --name <name>
knowledge sync --wiki-id <id>
knowledge search --wiki-id <id> --query <query>
```

`knowledge sync` uses the vendor service's `wiki/raw/write` and
`wiki/raw/reindex` APIs. It sends safe tracked text documents only. A service
search result is a candidate until the Python runtime validates its path and
commit against the current repository snapshot; this preserves the same
citation contract as the local index. CodeGraph is available through the
vendored service API, but is not fused into ordinary repository P@1 ranking.

The service is optional because its Wiki LLM ingest and CodeGraph native
module have additional Node dependencies. A healthy MemoryCore does not imply
that MemoryKnowledge is running; `doctor.knowledge_service` reports its own
configured/reachable state.

## Runtime checks

Run:

```bash
repository-memory doctor --json
```

The response contains `upstream_components` with the pinned commit, file count,
component roles, and the explicit fact that dirty upstream worktree changes
were excluded. The same field is emitted after installation because `vendor/`
is part of the Skill package.

The default runtime does not depend on the copied modules or an embedding
service. It uses the built-in `builtin-char-ngram-v1` local vector projection
and reports `retrieval_mode=local-hybrid`. The copied TencentDB modules remain
reference material and explicit compatibility code; they do not control the
standalone runtime.
