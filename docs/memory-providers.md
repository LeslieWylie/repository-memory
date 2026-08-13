# Memory providers

`repository-memory` is an independent local product. Its built-in SQLite
runtime owns the durable four-layer conversation memory plane and its local
vector projection. TencentDB MemoryCore and Memmy are optional compatibility
references only; neither is required to run the default CLI/MCP. Scores are
never fused across external provider lanes.

## Built-in standalone runtime

The default runtime implements the same useful lifecycle locally:

| Layer | What it stores | How it becomes trusted |
| --- | --- | --- |
| L0 | Raw conversation/messages | durable write and read-back |
| L1 | Atomic records projected from conversation | deterministic write and read-back |
| L2 | Scenario candidate generated from a session | explicit review/accept |
| L3 | Stable profile/core memory | explicit promotion and read-back |

The built-in vector projection is `builtin-char-ngram-v1`. It requires no model
download and is reported as `local-hybrid`; it must not be described as a
neural embedding model.

## Optional TencentDB compatibility backend

MemoryCore separates memory by lifecycle:

| Layer | What it stores | How it becomes trusted |
| --- | --- | --- |
| L0 | Raw conversation/messages | durable write and read-back |
| L1 | Atomic facts extracted from conversation | extraction and read-back |
| L2 | Scenario or generated context | candidate first, explicit review |
| L3 | Stable profile/core memory | explicit promotion and read-back |

The adapter probes the actual layer endpoints and returns the layer, record
status, provenance, and read-back receipt. A reachable API or an empty
scenario list is not reported as populated memory.

## Memmy

Memmy is used through its HTTP API; its source is not copied into this
repository. The adapter reuses the components that are useful to a generic
repository-memory host:

- SQLite persistence with FTS5 and native vector search;
- local embedding capability and its provider/model diagnostics;
- L1/L2/L3/Skill layer identity in search results;
- idempotency, change log, background jobs, retry, and import/export
  capabilities reported by health;
- an existing local panel, exposed by `repository-memory gui` rather than a
  second dashboard implementation.

Memmy's local embedding lane is kept separate from TencentDB's native
embedding setting. This means a live result can truthfully report:

```text
MemoryCore: keyword-only
Memmy: local-hybrid
repository: citation-first lexical/structured
```

`memmy status --json` also reports the configured summary/evolution model and
the last sanitized provider error. A configured model is not treated as a
successful generation: the current local probe can therefore show
`mlamp/gpt-5.6-luna` configured while still reporting a gateway quota error.
Local embedding/search remains independently usable in that state, but it is
not evidence that new L2/L3 memories were generated.

The unified runtime interleaves provider lanes without comparing scores. Each
result retains `source=memorycore` or `source=memmy`, its layer, memory id,
status, and provider citation. `scope=repository` never calls either memory
provider; `scope=memory` calls the configured memory providers; `scope=all`
returns separate repository and memory groups.

## Commands

```bash
repository-memory doctor --json
repository-memory memmy status --json
repository-memory memmy configure --endpoint http://127.0.0.1:18960 --json
repository-memory memmy search --query "..." --json
repository-memory gui --json
repository-memory gui --open
```

`memmy configure` writes only user-level configuration. It does not modify a
canonical repository. `gui --open` opens the configured Memmy endpoint on
macOS; without `--open`, it is a read-only reachability check.

## Failure and fallback rules

- If Memmy is stopped, the runtime reports the provider as unreachable and
  continues with the configured TencentDB/local lane.
- If TencentDB is stopped, repository search still works and memory results
  are marked unavailable rather than fabricated.
- If Memmy's summary/evolution model is unavailable, local embedding and
  existing records may still work; that does not prove new L2/L3 memories
  were generated.
- A Memmy memory is not a Git citation. It must not be used to answer a
  repository fact without a linked repository citation.
