# Upstream memory implementations we reviewed

The repositories below are reference snapshots only. They are kept outside
this repository under the user's local knowledge workspace so they cannot
silently become a second runtime or a second canonical source.

| Project | Reference commit | License | What we reuse | What stays out of the default install |
| --- | --- | --- | --- | --- |
| [MemOS](https://github.com/MemTensor/MemOS) | `b41c8996a8dcb9df81998cced68d11457ce950c3` | Apache-2.0; local plugin MIT | L1/L2/L3 vocabulary, ordered timeline, tool outcome feedback, lazy local model loading | Neo4j/Qdrant deployment, its competing OpenClaw plugin, relaxed induction thresholds |
| [Cognee](https://github.com/topoteretes/cognee) | `b948f88d48befe58e8b10e6b833adacdce4e0ddd` | Apache-2.0 | Explicit graph/relationship expansion as a future provider seam | Mandatory graph/vector service and a second document store |
| [TencentDB Agent Memory](https://github.com/Tencent/TencentDB-Agent-Memory) | `97f94654280b2932c35ba4806a491999ed244cc9` | MIT | Layer lifecycle and explicit read-back discipline | TencentDB service, Wiki/TCVDB and its memory slot |
| [Mem0](https://github.com/mem0ai/mem0) | `001c235229be8795e3834520467bd0d661ed8f34` | Apache-2.0 | Small provider boundary and feedback-oriented API shape | Hosted/vector database defaults and a competing memory authority |
| [MemPalace](https://github.com/MemPalace/mempalace) | `639c69a1d6be41a04964ceb72a3d29d6f45629e9` | MIT | Local-first retention and original-record preservation ideas | Its server deployment and unrelated personal-memory semantics |
| [Hindsight](https://github.com/vectorize-io/hindsight) | `ec9cc702ec55898bcac0db9c9e598305772ad7ad` | MIT | Retention/recall concepts are reference material only | No code copied into the runtime |

## Decision

We are not installing six plugins. `repository-memory` remains the only public
OpenClaw/MCP entrypoint. The Git repository and its commit/path/line citation
remain authoritative for project facts. Conversation memory remains a
separate local lane.

The first concrete reuse is in the standalone runtime:

- results expose `tier`, `ref_kind`, `ref_id`, and bounded `snippet` fields;
- `memory_timeline` exposes an ordered L0/L1 trace for diagnosis and replay;
- L2 is labelled `policy`, and L3 is labelled `world_model`, while the
  canonical L0/L1/L2/L3 names remain unchanged;
- all of this is local SQLite and does not require a daemon or model download.

The defaults from upstream were not copied blindly. In particular, a low
induction threshold can create impressive-looking but noisy L2/L3 records. Our
runtime keeps explicit candidate/accepted states and read-back verification.

## Reference locations

The clone locations are user-level reference material, not import paths used
by the runtime:

```text
/Users/mlamp/Desktop/03-Knowledge/MemOS-reference
/Users/mlamp/Desktop/03-Knowledge/memory-references/cognee
/Users/mlamp/Desktop/03-Knowledge/memory-references/tencentdb-agent-memory
/Users/mlamp/Desktop/03-Knowledge/memory-references/mem0
/Users/mlamp/Desktop/03-Knowledge/memory-references/mempalace
/Users/mlamp/Desktop/03-Knowledge/memory-references/hindsight
```

Do not add these absolute paths to Skill instructions or user configuration.
