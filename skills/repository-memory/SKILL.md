---
name: repository-memory
description: Find source-backed project facts and durable conversation memory with verified citations, freshness status, and explicit write boundaries.
---

# Repository Memory

Use this Skill when the user asks about project knowledge, research notes,
reports, repository history, source-level evidence, or explicitly imported
long-term memory. It is a generic local Skill: discover the repository,
adapter, runtime, and index at execution time. Do not invent paths, providers,
models, ports, or service URLs.

## Required first-use flow

Use the host's namespaced repository-memory MCP when available. The public MCP
tools are:

```text
repository-memory__memory_doctor
repository-memory__memory_sync
repository-memory__memory_search
repository-memory__memory_get
```

The exact namespace is host-defined. A bare `memory_search` may belong to a
different backend; do not silently substitute it.

Before the first query, or after the environment changes:

1. Call `memory_doctor` (or the bundled CLI `doctor --json`).
2. Confirm the actual source, indexed commit, freshness, adapter, retrieval
   mode, and memory-layer population. `ready` means usable, not populated.
3. If the source is missing or stale, call `memory_sync`, then run doctor
   again. Do not claim a stale or dirty checkout is fresh.
4. For a new installation, run one real positive query and one fabricated
   negative query before reporting the setup as working.

If the MCP and bundled CLI are both absent, report `not_installed`. A config
file or a model-written receipt is not proof of installation.

## Query flow

For ordinary project questions, preserve the user's wording and call:

```text
memory_search(query=<user's original question>, scope="repository")
```

Do not manually turn a natural-language question into a filename. The runtime
handles conservative lexical, structural, date, and local semantic routing.
Use `scope="memory"` only for conversation memory, and `scope="all"` when the
user explicitly wants both. Repository evidence and conversation memory must
remain separate; never present a memory record as a Git citation.

After search:

1. Prefer `answerable`/`results`, not merely the presence of `verified`.
2. For the selected result, call `memory_get` when the claim is important,
   compound, partial, or time-sensitive.
3. Answer only from evidence whose citation is valid and whose freshness is
   acceptable. Report source/repository, commit, path or memory layer, line
   range or locator, freshness, and evidence status.
4. If `claim_support` is `partial` or `unknown`, state only the supported
   subclaim and abstain from the rest.
5. If no directly supported result exists, say that the evidence is
   insufficient. Do not fill the gap with a similar document.

`verified` means the runtime checked a citation or a stable memory
layer/identifier. It does not mean one excerpt proves every part of a
compound question. `candidates` are leads only: stale, dirty, generated,
inferred, pending, or citation-incomplete content is not a fact until it is
validated with `memory_get`/`explain` and the lifecycle rules allow it.

When semantic dependencies are unavailable, report the actual
`retrieval_mode` (for example `lexical-fallback` or `keyword-only`). Never
call keyword retrieval semantic or hybrid. The runtime may use a local,
dependency-free projection, but its availability must come from doctor/search.

## Memory layers and writes

The standalone runtime keeps these layers distinct:

```text
L0  raw conversation/message       explicit ingest or opt-in capture
L1  extracted atomic memory         read-back verified before use
L2  scenario/context                candidate until explicit review
L3  profile/core memory             explicit accept/promote plus read-back
```

An API being reachable does not prove that a layer contains useful data. Use
doctor or get to distinguish capability, readiness, population, status, and
read-back. Do not claim L2/L3 exists merely because the endpoint supports it.

Normal search and sync are read/index operations. Only run `ingest-session`,
`feedback`, `promote`, Team Memory publish/activate, or host capture when the
user or an explicit task-end workflow requests a write. Never silently rewrite
the canonical repository. New memories remain candidate/pending until the
documented review or promotion step; ordinary conversation never writes L3.

## Tool boundary

The Skill does not forbid normal development tools. `read`, `grep`, `git`,
`exec`, tests, and debugging remain valid for coding tasks. For a project-fact
answer, however, do not use them as an undocumented substitute for the
repository-memory MCP, and do not describe directly inspected files as MCP
citations. If the source is not registered, explicitly register it with the
CLI only after the user supplies or confirms the repository root.

Read the references only when needed:

- `references/operations.md` — CLI commands and MCP handshake;
- `references/result-contract.md` — `verified`, `answerable`, and candidates;
- `references/citation.md` — citation validation;
- `references/entity-and-memory-model.md` — routing and L0–L3 semantics;
- `references/write-policy.md` — explicit writes and promotion;
- `references/automatic-capture.md` — optional host lifecycle capture;
- `references/team-memory.md` — shared decisions and handoffs.
