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

## MemOS Local lifecycle ideas incorporated

The public repository does not depend on the MemOS Node package, but the
standalone runtime now incorporates the useful local-plugin mechanics in
`memos_lifecycle.py`:

- real episode/turn identifiers on L0/L1 records;
- conservative revision/follow-up/new-task boundaries;
- feedback-weighted value and time-decayed priority;
- an L2 candidate pool that needs evidence from at least two independent
  episodes and keeps the supporting record IDs;
- a read-only timeline and dashboard over the same SQLite runtime.

These are deliberately ported into the Python runtime so installation remains
one-command and dependency-free. Provider daemons, OpenClaw-specific bridges,
and MemOS's Node dependency tree are not required. The algorithmic reference is
the Apache-2.0 MemOS Local Plugin source, read from a local checkout of its
upstream repository
[MemOS Local Plugin](https://github.com/MemTensor/MemOS/tree/main/apps/memos-local-plugin).
The reference checkout is not part of the production runtime.

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
standalone-memory: local-hybrid (built-in local vector projection)
optional MemoryCore compatibility: keyword-only or provider-reported
optional Memmy compatibility: local-hybrid when its local model is available
repository: citation-first lexical/structured
```

`memmy status --json` also reports the configured summary/evolution model and
the last sanitized provider error. A configured model is not treated as a
successful generation: the current local probe can therefore show
`<provider>/<model>` configured while still reporting a gateway quota error.
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
repository-memory doctor --local --json
repository-memory memmy status --json
repository-memory memmy configure --endpoint http://127.0.0.1:18960 --json
repository-memory memmy search --query "..." --json
repository-memory gui --json
repository-memory gui --open
repository-memory gui --serve --open
repository-memory memory evolve --json
repository-memory feedback local:L2:policy:<id> --rating helpful --note "reused"
```

`memmy configure` writes only user-level configuration. It does not modify a
canonical repository. `gui --open` opens the configured Memmy endpoint on
macOS; without `--open`, it is a read-only reachability check. `gui --serve`
starts the built-in standard-library-only read-only dashboard, so the public
product has a usable local GUI even when Memmy is absent. It calls the same
doctor/search/memory runtime as CLI and MCP and does not add a write path.

## Optional neural retrieval

The default `builtin-char-ngram-v1` projection is dependency-free and is
reported as `local-hybrid` with `native_neural_model=false`. For a multilingual
neural A/B, install the optional dependency in the target environment and
explicitly configure the model:

```bash
python -m pip install "sentence-transformers>=3.0"
repository-memory semantic configure --model Alibaba-NLP/gte-multilingual-base
repository-memory semantic status --json
```

If the model or dependency is unavailable, search remains usable and reports
`lexical-fallback`; it never claims a neural encoder is active. An isolated
benchmark can compare a model without changing user configuration:

```bash
repository-memory benchmark --suite public \
  --semantic-model Alibaba-NLP/gte-multilingual-base --json
```

Use `--semantic-download` only when downloading during that benchmark is
explicitly intended.

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
