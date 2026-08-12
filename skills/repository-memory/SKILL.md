---
name: repository-memory
description: Search durable project and research memory across discovered knowledge sources and repositories with source-backed citations, freshness diagnostics, and explicit candidate-writing boundaries. Use when an agent needs verified project facts, long-lived knowledge, relationships, recent reports, or source-level evidence.
---

# Repository Memory

Use this Skill for durable repository knowledge, shared team experience, and
explicitly imported long-term memory, not transient conversation context. The bundled runtime discovers
sources and adapters at execution time; do not invent paths, providers, models,
indexes, or deployment details.

Prefer the host's registered stdio MCP. When using the CLI, invoke the bundled
`scripts/repository-memory` (or `scripts/repository-memory.py`) relative to this Skill's
installed directory; do not assume a global executable or a particular current
working directory.

On hosts that namespace MCP tools, the repository-memory tools are exposed with
the host's repository namespace (for example, `repository-memory__memory_search`).
The bare host tool `memory_search` is a different backend. Prefer the
namespaced tools for shared evidence, but the host guard is advisory and must
not block ordinary `read`, `grep`, `git`, `exec`, tests, or debugging. If a
memory backend is unavailable, report that fact and distinguish any direct
workspace inspection from retrieved memory evidence.

## First-use setup handshake

This Skill is a usage contract, not proof that a host has installed the Skill
or registered its MCP. Before claiming that setup is complete:

1. Confirm that the Skill is present and that the host exposes the repository
   memory MCP. If neither the MCP nor the bundled CLI is available, report
   `not_installed` and ask the operator for the Skill package/registration;
   never claim that configuration succeeded.
2. Run `memory_doctor` (or the bundled CLI's `doctor --json`). Record the
   actual adapter, sources, index, freshness, MemoryCore layers, and semantic
   capability.
3. If no repository source is configured and the operator supplied a Git root,
   call `memory_init` with that path. Otherwise ask which repository is the
   intended knowledge source. Do not silently register an arbitrary current
   directory.
4. If the selected source is stale or missing an index, call `memory_sync` and
   re-run `memory_doctor`. Do not use local dirty state as fresh remote evidence.
5. Verify the MCP capability with `tools/list` when available, then run one
   positive citation query and one clearly fabricated negative query. Claim
   `ready` only when the observed responses support it; include any degraded
   or fallback state in the report.

Read [operations](references/operations.md) for the exact tool/CLI handshake
and [entity-and-memory-model](references/entity-and-memory-model.md) before
explaining entity extraction or session-memory writes.

If the host supports a lifecycle extension, read
[automatic-capture](references/automatic-capture.md). A post-turn hook may
capture a bounded user/assistant turn into L0 and create an untrusted L2
candidate, but this is an installation capability rather than a promise made
by the Skill alone. Plain MCP/CLI use remains read-only unless the user
explicitly requests a write.

## Operating procedure

1. Run `doctor --json` before the first query or when the environment changes.
2. If no source is configured, ask for or discover the user's intended knowledge directory, then run `init --path <directory> --json`. Repeat `source add --path <directory> --id <stable-id>` for additional repositories or document roots. This writes only user config and derived cache.
3. Run `sync --json` when the doctor reports a missing/stale index or the source is not fresh.
4. For a task that may benefit from prior agent work, call `memory_context(query)` first. It returns repository evidence plus shared decisions, failures, solutions, discoveries, and handoffs with separate provenance. Its current mode is `multi-source-lexical`: the lanes run in parallel but are not score-fused. Use `search "<question>" --scope repository --json` for a repository-only lookup, or `--scope memory` when the native conversation layers are explicitly needed.
5. Inspect `verified`, `candidates`, `support`, `freshness`, and `diagnostics`; use `get` or `explain` for the complete evidence window and pass the result's citation `commit` when available.
6. Cite only `verified` results with a valid citation. A verified document proves that the document citation is real; `support.claim_support=partial|unknown` means the answer must abstain from the unsupported part of a composite claim.

For a project-fact turn, the auditable call sequence is:

```text
repository-memory doctor → repository-memory context/search → repository-memory get
```

The host extension records this sequence when it can observe it, but does not
block coding or debugging tools. A model-written receipt is not evidence that
the sequence happened; use the host audit record when it is available.

Do not manually rewrite a natural-language query into a filename or path. The
runtime performs conservative CJK token expansion, entity-in-filename matching,
and temporal routing to report/standup layers. If it still returns no verified
evidence, mark the retrieved claim unknown. Direct workspace inspection may be
used when it is part of the coding task, but it must not be mislabeled as a
citation returned by this Skill.

The repository runtime's current “entity extraction” is structural lexical
routing, not named-entity recognition and not a persistent relation graph. It
uses the original query, conservative token forms, source path/file name,
headings, dates, and generic directory layers. Do not invent aliases,
relationships, or entity facts that the result does not expose.

When using MCP, do not invent or pass a `root` path. Use the server's configured
repository discovery unless the user explicitly supplies a Git repository root;
pass only the query and, when needed, a known source id.

`candidates` are leads, not facts. Generated, inferred, pending, stale, dirty, or citation-incomplete repository results require source verification. Team Memory uses its own lifecycle: `active` records are experience/decision context with provenance, while `candidate`, `stale`, and `superseded` records are not facts. `memory_context` keeps the sections separate and never makes a team recollection look like a Git citation.

Normal use is read-only. Only run `memory_publish`, `memory_feedback`,
`memory_supersede`, `feedback`, `promote`, or `memory_ingest` when the user or
an explicit task-end workflow asks for a write. New Team Memory defaults to
`candidate`; promotion/replacement is explicit. Use the local stdio MCP
entrypoint when the host exposes it; CLI and MCP return the same contract.
When agents run on different hosts, use the explicit `team-export` and
`team-import` bundle commands (or `memory_team_sync`) to transfer Team Memory;
review candidates explicitly with `team-activate`/`memory_team_activate`, and
do not claim that a local SQLite file is automatically cross-machine shared.

When the host exposes the audited MCP registration, tool calls are recorded as
metadata-only request/response events. Treat `audit_verified` evidence from the
host trace as stronger than a model-written receipt.

If the user explicitly asks to import a conversation or session, use the CLI
`ingest-session` path or the explicit MCP `memory_ingest` tool. Treat its output
as a write operation and check the reported memory-layer and pipeline status.
An installed lifecycle extension may separately capture a completed host turn;
that capture is still a write and must be reported as L0 verified, L1
pending/verified, and L2 candidate until a human accepts it. It never writes
L3 by itself. A native memory service, if configured, may expose L0-L3. The
current native write contract verifies the durable L0 conversation and normally
reports L1 extraction as `pending` until a later observation confirms it; it
does not promise synchronous L2/L3 creation. Only report L2/L3 as populated
when the adapter actually returns those records and their status. Otherwise
the runtime's local durable backend proves only L0/L1 and reports L2/L3 as
unsupported; do not upgrade either state to a richer memory system in the
answer.

Read the relevant reference only when needed:

- [retrieval](references/retrieval.md) for routing and freshness decisions;
- [result contract](references/result-contract.md) for `verified`/`candidates` handling;
- [citation](references/citation.md) for source validation;
- [write policy](references/write-policy.md) for explicit writes;
- [operations](references/operations.md) for CLI and MCP invocation.
- [entity and memory model](references/entity-and-memory-model.md) for the
  current entity-routing and L0-L3 semantics.
- [automatic capture](references/automatic-capture.md) for host lifecycle
  capture, redaction, candidate review, and explicit L3 promotion.
- [team memory](references/team-memory.md) for shared memory types, lifecycle,
  context hydration, feedback, and conflict resolution.
