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

## What was added after the comparison

The useful parts are implemented in the independent local core rather than
left as adapter promises:

- MemOS-style lifecycle metadata is represented by layer, status, source,
  session, version and read-back fields; feedback remains explicit and does not
  silently rewrite a fact.
- Hindsight's separation between retention, recall and reflection is reflected
  in separate `ingest-session`, `search`, `timeline` and supervisor paths.
  Reflection/supervision remains optional and cannot auto-accept L2/L3.
- Cognee's explainable relationship idea is implemented as a tiny derived graph:
  Markdown links and explicit local file references are indexed, relationship
  queries expand one hop, and results expose `related` citations. There is no
  graph server and no inferred edge pretending to be evidence.
- Mem0-style identity/session/run scoping and deduplication are retained in the
  standalone memory and team-memory stores.
- TencentDB's layer readiness versus population/read-back distinction is kept
  as a hard contract: an available endpoint is not counted as useful memory.
- TencentDB's capture boundary is now implemented in the public OpenClaw
  extension: when the host exposes a pre-turn message count or timestamp
  cursor, capture keeps only the new turn, restores the original user text,
  and removes injected recall from the durable input. Hosts that do not expose
  those fields retain the bounded safe fallback.
- MemOS' short-CJK retrieval lesson is implemented without importing its
  runtime: the derived trigram index includes the relative path as searchable
  text, so a person/card name that appears only in a filename can still reach
  the citation-first scorer. This is a recall fix, not a semantic claim.
- MemOS' hybrid lane is also used as an explicit rescue path rather than an
  always-on startup cost: large sources begin with lexical/path retrieval and
  load the optional deterministic projection only after a lexical miss. This
  preserves the useful fallback without making a fresh snapshot appear hung.

The repository index stores conservative date anchors from paths and headings,
plus explicit local references. Temporal routing uses those anchors instead of
dates buried in arbitrary evidence text. This improves latest/report queries
without changing canonical Git files.

## Deliberate non-goals

We did not copy Neo4j/Qdrant/Postgres deployment, a second canonical document
store, opaque graph edges, automatic LLM memory acceptance, or a mandatory
remote embedding service. The default remains zero-service and citation-first.
The current `builtin-char-ngram-v1` projection is a deterministic local recall
lane; it is not a neural embedding model and is reported as such.

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
