# Operations

Use the bundled CLI entrypoint relative to this Skill's installed directory
(`scripts/repository-memory` or `scripts/repository-memory.py`) with a repository root
discovered from the current directory, user configuration, or an explicit
`--root`. A host-provided MCP is preferred when available.

## Host setup handshake

Before running a query, establish which interface is actually available:

1. Check that the Skill package is loaded and inspect the registered MCP with
   `tools/list` when the host supports MCP discovery.
2. If the Skill, CLI, and MCP are all absent, stop with
   `status=not_installed`; do not infer a successful installation from a
   configuration file or a model-written receipt.
3. Call `memory_doctor` or `doctor --json` and retain its real adapter/source,
   index, freshness, MemoryCore, and semantic fields.
4. Call `memory_init` only after the operator has supplied the repository root.
   Do not register an arbitrary current directory merely to make doctor look
   ready.
5. Call `memory_sync` for a missing/stale index, then repeat doctor. Finish
   setup with one positive citation query and one fabricated negative query.

The setup result is `ready` only when the observed MCP/CLI calls support that
claim. `not_installed`, `not_configured`, `unreachable`, `degraded`, `dirty`,
and `fallback` remain explicit statuses.

```text
repository-memory doctor --json
repository-memory init --path <knowledge-directory> [--id <stable-id>] [--local-only] --json
repository-memory source add --path <knowledge-directory> [--id <stable-id>] [--local-only] --json
repository-memory source list --json
repository-memory sync [--source <id>|--all] [--local] --json
repository-memory search "<query>" [--source <id>] [--scope repository|memory|all] [--deep] [--local] --json
repository-memory get "<result-id>" [--commit <citation-commit>] --json
repository-memory explain "<result-id>" [--commit <citation-commit>] --json
repository-memory feedback "<result-id>" --note "..." [--feedback-id <stable-id>] --json
repository-memory promote --input <file> --json
repository-memory ingest-session --input <json-or-jsonl> --json
repository-memory capture-turn --input <bounded-turn.json> --json  # lifecycle adapter only
repository-memory team-export --output <bundle.json> --json
repository-memory team-import --input <bundle.json> --json
repository-memory team-activate --id <team-memory-id> [--reviewer <agent>] --json
repository-memory memorycore promote-l3 --candidate <autocapture:L2:id> --accept --json
repository-memory evaluate --queries <queries.jsonl> --qrels <qrels.jsonl> [--revision <commit>] [--scope repository|memory|all] --json
repository-memory team-evaluate --records <records.jsonl> --queries <queries.jsonl> --qrels <qrels.jsonl> [--gate] --json
repository-memory team-compact [--keep N] --json
repository-memory memorycore configure|start|stop|status
repository-memory mcp
```

The installer may place a transparent metadata-only proxy in front of the MCP
server. It writes request/response events to the user data directory's
`audit.jsonl`; it records tool names, query hashes, counts, freshness states,
and latency, but not full queries, excerpts, or response bodies. Use host
transcripts or this audit stream to verify actual MCP usage; a model-written
receipt alone is not proof.

The MCP entrypoint uses local stdio and exposes the read/query tools plus
explicit setup and session-ingest tools with the same JSON contract:

```text
memory_doctor
memory_sync
memory_search
memory_get
memory_init
memory_ingest
memory_context
memory_team_sync
memory_team_activate
memory_publish
memory_feedback
memory_supersede
```

`memory_init` registers a user-provided knowledge directory and builds a
disposable local lexical index. It may be called repeatedly for different
sources; source IDs are stable handles, not assumptions about a particular
repository. It never edits canonical documents. `memory_ingest` is an explicit
write and accepts a session object or JSONL value; it is not part of ordinary
retrieval.

`team-export`/`team-import` and `memory_team_sync` move the user-level Team
Memory plane as an explicit JSON bundle. They are idempotent merge operations,
not repository snapshot sync and not a claim that a hosted cross-machine
service exists.

The native ingest response is intentionally conservative: it can verify the
durable L0 conversation write while reporting L1 extraction as `pending` or
`unknown`. L2/L3 are not promised by the write response; report them only after
the corresponding read/search API returns a record and status. A native
backend's `supported_layers`/`reachable` fields describe capability and
readiness, not the amount or quality of stored memory.

`sync` updates only remote snapshots and derived indexes. It does not pull, commit, push, or overwrite the working tree. Use `--local` only when local checkout state is intentionally desired. For an intentionally detached or offline snapshot, register the source with `--local-only`; this makes the configured local commit the declared source of truth and reports `commit_type=local_worktree` with `freshness.state=fresh` when the checkout is clean. It does not claim that the snapshot is the latest remote revision. Dirty local-only sources remain `dirty` and are not verified.

`evaluate --revision` evaluates an immutable detached snapshot in the user
cache and records the evaluated commit, branch, qrels revision, scope, and
retrieval mode. It never compares an unpinned working tree with a moving remote
branch and never modifies the canonical repository.

`ingest-session` is an explicit write operation. It passes generic session JSON or
JSONL through the selected adapter's conversation-to-memory pipeline when that
adapter provides one. It may write the adapter's configured canonical memory
store, so it must only be run after the user explicitly requests ingestion.
For native conversation stores, a successful response verifies the durable L0
write; L1 extraction may be asynchronous and must remain `pending`/`unknown`
until a later search or doctor call observes it. Do not report L1 as complete
just because the HTTP mutation returned successfully. If no native or external
adapter is available, the runtime persists a local L0 raw record and
deterministic L1 atomic record in the user data directory; the result identifies
`local-memory` and its actual layer support.

`capture-turn` is not an ordinary query command. A host lifecycle extension may
invoke it after a successful turn with bounded user/assistant messages. It
redacts and de-duplicates the payload, verifies L0, observes L1, and writes only
an unaccepted L2 candidate. `memorycore promote-l3` is a separate explicit
review operation; it reads the candidate, writes L3, reads L3 back, and archives
the candidate outside the pending search tree. It requires `--accept` and is
not exposed as a normal MCP tool.

An adapter is selected dynamically. Its minimum JSON protocol is `doctor --json`, `sync --json`, `search --query ... --json`, and `get --id ... --json`; optional capabilities are reported by `doctor`. The Skill does not assume a vendor, provider, model, URL, or semantic index.

When the selected adapter reports a conversation memory plane, `doctor` exposes
its supported layers, configured state, live reachability, credential source,
and whether retrieval is keyword-only. Search preserves the returned memory
layer/type and query source in each result. Native MemoryCore citations use a
stable memory id, layer, and returned evidence instead of pretending that a
conversation record is a Git file. A layer label is metadata, not proof:
citation validation still controls whether the result is `verified` or remains
a candidate.

For MCP calls, omit `root` by default. The server process owns the configured
root and source discovery; only pass an explicit root when the user has named a
Git repository and the caller has verified that path.
