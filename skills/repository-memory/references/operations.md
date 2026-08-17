# Operations

Use the bundled CLI entrypoint relative to this Skill's installed directory
(`scripts/repository-memory` or `scripts/repository-memory.py`) with a repository root
discovered from the current directory, user configuration, or an explicit
`--root`. A host-provided MCP is preferred when available.

## Host setup handshake

For a remote OpenClaw host, use the repository's bootstrap command instead of
asking the agent to assemble separate clone/install/MCP commands:

```text
curl -fsSL https://raw.githubusercontent.com/LeslieWylie/repository-memory/main/bootstrap.sh | sh -s -- --target openclaw --openclaw-agent auto --source-url <knowledge-git-url> --source-branch main --json
```

`auto` resolves the current agent from `OPENCLAW_AGENT_ID`/`AGENT_ID` or from
the only configured agent. If there are multiple configured agents, the
installer fails with their ids rather than silently installing to the wrong
profile. Use `--openclaw-all-agents` only when that scope is intentional.
The bootstrap checkout and source checkout are user-cache data; the source
repository is never modified, committed, or pushed by installation.

Before running a query, establish which interface is actually available:

1. Check that the Skill package is loaded and inspect the registered MCP with
   `tools/list` when the host supports MCP discovery.
2. If the Skill, CLI, and MCP are all absent, stop with
   `status=not_installed`; do not infer a successful installation from a
   configuration file or a model-written receipt.
3. Call `memory_doctor` or `doctor --json` and retain its real adapter/source,
   index, freshness, MemoryCore, and semantic fields.
4. Call the CLI `init --path <root>` only after the operator has supplied the
   repository root. Do not register an arbitrary current directory merely to
   make doctor look ready.
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
repository-memory get "<result-id>" [--commit <citation-commit>] [--line-start N --line-end N] --json
repository-memory explain "<result-id>" [--commit <citation-commit>] [--line-start N --line-end N] --json
repository-memory feedback "<result-id>" --note "..." [--feedback-id <stable-id>] --json
repository-memory promote --input <file> --json
repository-memory ingest-session --input <json-or-jsonl> --json
repository-memory capture-turn --input <bounded-turn.json> --json  # lifecycle adapter only
repository-memory team-export --output <bundle.json> --json
repository-memory team-import --input <bundle.json> --json
repository-memory team-activate --id <team-memory-id> [--reviewer <agent>] --json
repository-memory memory promote-l3 --candidate <autocapture:L2:id> --accept --json
repository-memory evaluate --queries <queries.jsonl> --qrels <qrels.jsonl> [--revision <commit>] [--scope repository|memory|all] --json
repository-memory semantic status --json
repository-memory semantic configure --model <hugging-face-model-id> [--download] --json
repository-memory team-evaluate --records <records.jsonl> --queries <queries.jsonl> --qrels <qrels.jsonl> [--gate] --json
repository-memory team-compact [--keep N] --json
repository-memory memorycore configure|start|stop|status
repository-memory knowledge status|configure|install|start|stop|create|sync|search
repository-memory mcp
```

The installer may place a transparent metadata-only proxy in front of the MCP
server. It writes request/response events to the user data directory's
`audit.jsonl`; it records tool names, query hashes, counts, freshness states,
and latency, but not full queries, excerpts, or response bodies. Use host
transcripts or this audit stream to verify actual MCP usage; a model-written
receipt alone is not proof.

The MCP entrypoint uses local stdio and exposes only the read/diagnostic tools
with the same JSON contract. Explicit setup, session ingest, review, feedback,
and promotion remain CLI operations so an agent cannot mutate memory merely by
having access to the query MCP:

```text
memory_doctor
memory_sync
memory_search
memory_get
```

Use the CLI `init`, `source add`, `ingest-session`, `feedback`, `promote`, and
the team-memory commands for explicit writes. They may update user config,
derived cache, or user-level memory state, but never canonical documents unless
the operator separately commits an approved repository change.

`team-export`/`team-import` move the user-level Team Memory plane as an explicit
JSON bundle. They are idempotent merge operations, not repository snapshot sync
and not a claim that a hosted cross-machine service exists. The corresponding
Team Memory MCP write/sync tools are intentionally not in the public tool list.

The native ingest response is intentionally conservative: it can verify the
durable L0 conversation write while reporting L1 extraction as `pending` or
`unknown`. L2/L3 are not promised by the write response; report them only after
the corresponding read/search API returns a record and status. A native
backend's `supported_layers`/`reachable` fields describe capability and
readiness, not the amount or quality of stored memory.

`sync` updates only remote snapshots and derived indexes. It does not pull, commit, push, or overwrite the working tree. Use `--local` only when local checkout state is intentionally desired. For an intentionally detached or offline snapshot, register the source with `--local-only`; this makes the configured local commit the declared source of truth and reports `commit_type=local_worktree` with `freshness.state=fresh` when the checkout is clean. It does not claim that the snapshot is the latest remote revision. Dirty local-only sources remain `dirty` and are not verified.

The optional TencentDB MemoryKnowledge plane is separate from MemoryCore. Use
`knowledge status` to check it; use `knowledge create`, `knowledge sync`, and
`knowledge search` only when a Wiki asset is explicitly configured. Knowledge
service results remain candidates until the repository runtime validates a Git
path, commit, line range, and excerpt. A ready MemoryCore endpoint does not
mean that Wiki or CodeGraph is populated.

`verified` means the document citation is real and traceable; it does not mean
the excerpt supports every part of a composite question. The answer-safe
surface is `answerable` (also returned as `results`), which contains only
directly supported claims. If `verified` is non-empty but `answerable` is
empty, preserve the citations for diagnosis/evaluation and abstain from the
complete claim. Pass the returned commit and line range to `get`/`explain` to
inspect the exact evidence window instead of silently reading a document's
first page.

`evaluate --revision` evaluates an immutable detached snapshot in the user
cache and records the evaluated commit, branch, qrels revision, scope, and
retrieval mode. It never compares an unpinned working tree with a moving remote
branch and never modifies the canonical repository.

`ingest-session` is an explicit write operation. It passes generic session JSON or
JSONL through the selected adapter's conversation-to-memory pipeline when that
adapter provides one. It may write the adapter's configured canonical memory
store, so it must only be run after the user explicitly requests ingestion.
For the standalone conversation store, a successful response verifies durable
L0 and deterministic L1 by read-back. It creates no accepted L2/L3 implicitly;
L2 remains a candidate until explicit review and L3 requires explicit
promotion/read-back. An optional vendor backend may report asynchronous L1, but
that external mode is never needed for a fresh install.

`memory project` is an explicit local maintenance operation that projects
already stored L0 conversations into reviewable L2 candidates. It is
idempotent and never accepts those candidates or writes L3. Use
`memory promote-l3 --candidate <id> --accept` only after reviewing one.

`capture-turn` is not an ordinary query command. A host lifecycle extension may
invoke it after a successful turn with bounded user/assistant messages. It
redacts and de-duplicates the payload, verifies L0, observes L1, and writes only
an unaccepted L2 candidate. `memory promote-l3` is a separate explicit
review operation; it reads the candidate, writes L3, reads L3 back, and archives
the candidate outside the pending search tree. It requires `--accept` and is
not exposed as a normal MCP tool.

The built-in standalone runtime is the default execution path. Its public
protocol is `doctor --json`, `sync --json`, `search --query ... --json`, and
`get --id ... --json`; optional compatibility backends are only selected from
explicit user configuration. The Skill does not require a vendor, provider,
model, URL, or external semantic service.

The standalone doctor exposes supported layers, local persistence, vector
provider/model/dimension, and lifecycle counts. Search preserves the memory
layer/type and query source in each result. A stable memory id, layer, and
returned evidence are used instead of pretending that a conversation record is
a Git file. A layer label is metadata, not proof: lifecycle status still
controls whether the result is `verified` or remains a candidate.

For MCP calls, omit `root` by default. The server process owns the configured
root and source discovery; only pass an explicit root when the user has named a
Git repository and the caller has verified that path.
