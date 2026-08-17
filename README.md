# Repository Memory

Repository Memory is a small, source-backed memory layer for AI agents.
It is standalone by default: the Python process owns the local SQLite memory
store and repository index, so a fresh install does not require TencentDB,
Memmy, Node, an embedding service, or an API key.
It gives an agent one consistent way to answer questions about project
documents, research notes, reports, source-code evidence, and explicitly
imported conversation memory.

The important idea is simple:

> An answer is a fact only when the runtime can show where it came from.

It ships as one generic Skill with a shared Python runtime and a pinned,
source-only upstream component reference snapshot:

- a citation-first repository index and CLI;
- a local stdio MCP server for Claude, Codex, OpenClaw, and other hosts;
- an in-process L0-L3 conversation memory runtime, with vendor backends only as
  explicit compatibility options;
- an OpenClaw lifecycle extension for before-prompt recall and conservative post-turn capture;
- a metadata-only audit proxy and an advisory guard that validates evidence
  receipts without blocking normal coding, shell, Git, or debugging tools.

The mainline is citation-first repository retrieval. An optional user-level
Team Memory plane can store compact decisions, failures, discoveries, solutions,
and handoffs, but it remains separate from Git citations and is never required
for repository search.

The repository itself is the source of truth. Indexes, snapshots, audit logs,
conversation data, and credentials stay in user-level data/config/cache
directories and are never written back to the source repository by search or
sync.

## What happens on one question?

```mermaid
flowchart LR
    A[Agent question] --> B[MCP or CLI]
    B --> C[doctor and scope router]
    C --> D[Repository snapshot and structured index]
    D --> E[Verified citation: commit, path, lines]
    C --> F[Built-in standalone memory]
    F --> G[L0 raw conversation]
    F --> H[L1 atomic memory]
    F --> I[L2 scenario candidate or accepted]
    F --> J[L3 profile/core after explicit promotion]
    E --> K[Answer or abstain]
    G --> K
    H --> K
    I --> K
    J --> K
    B --> L[Optional audit and advisory guard]
    C --> M[Shared Team Memory context]
    M --> N[Decisions, failures, solutions, handoffs]
```

`scope=repository` searches Git-backed evidence only. `scope=memory` searches
the configured conversation-memory plane. `scope=all` returns two separate
groups; it never fuses scores or turns a conversation into a Git citation.
For multi-agent work, the CLI `memory_context` command can package repository
evidence and Team Memory sections together without making experience look like
a Git citation. The public MCP surface remains deliberately read-only and uses
`memory_search` with an explicit `scope` when native memory is needed.
`memory_timeline` is also available for ordered L0/L1 trace inspection; it is
diagnostic provenance, not repository evidence and cannot promote L2/L3.

## Install

Requirements: Python 3.10+ and Git. The core runtime uses only the Python
standard library. Node.js is needed only for the OpenClaw extension tests or
when OpenClaw itself requires it.

```bash
git clone https://github.com/LeslieWylie/repository-memory.git
cd repository-memory

# Install the Skill, CLI, MCP registration, and (when OpenClaw is configured)
# the profile-local lifecycle extension.
python3 install.py --all --openclaw-agent <agent-id> \
  --source-root /path/to/knowledge-repository --json
```

For a single host:

```bash
python3 install.py --target codex --source-root /path/to/knowledge-repository --json
python3 install.py --target claude --source-root /path/to/knowledge-repository --json
python3 install.py --target openclaw --openclaw-config /path/to/openclaw.json \
  --openclaw-agent <agent-id> \
  --source-root /path/to/knowledge-repository --json
```

For a remote OpenClaw host, the shortest supported bootstrap is one command:

```bash
curl -fsSL https://raw.githubusercontent.com/LeslieWylie/repository-memory/main/bootstrap.sh \
  | sh -s -- --target openclaw --openclaw-agent auto \
  --source-url <knowledge-git-url> --source-branch main --json
```

`--openclaw-agent auto` uses `OPENCLAW_AGENT_ID`/`AGENT_ID`, or selects the
only configured OpenClaw agent. If the profile intentionally contains several
agents, replace it with `--openclaw-all-agents` or an explicit repeated
`--openclaw-agent <id>`. `--source-url` clones the knowledge repository into a
user cache, registers it, and runs the same doctor/MCP smoke checks. Credentials
are taken from the host's existing Git credential helper; they are not written
to the command, config, or audit log.

OpenClaw installation is least-privilege by default: `--openclaw-agent` is
required and only that agent receives the Skill, MCP tool permissions, and
automatic capture. Use `--openclaw-agent` repeatedly for a selected set. The
all-agent behavior is available only through the explicit
`--openclaw-all-agents` flag.

The installer makes a timestamped backup before changing a host config. It
does not push, commit, pull, or rewrite the knowledge repository.

## First check

After installation, run the bundled executable or the generated user-level
command:

```bash
repository-memory doctor --json
repository-memory search "the question in the user's own words" \
  --scope repository --json
```

With OpenClaw, verify the registered server through the host rather than
trusting a model-written receipt:

```bash
openclaw mcp probe repository-memory
```

A healthy repository setup reports an indexed commit, a non-stale source, and
results containing a valid `citation`. If the source is missing, stale, dirty,
or the citation cannot be checked, the result stays in `candidates` or the
runtime returns `abstain=true`.

## Result rules

Every search response has two layers:

- `verified`: the runtime resolved the source, commit, path, line range, and
  excerpt, and no disqualifying status was found;
- `candidates`: related or incomplete material, including stale, generated,
  inferred, pending, dirty, or citation-incomplete results.

Agents should answer from `answerable` (also returned as `results`) only.
`verified` is the document-retrieval/evaluation lane: it proves that a citation
is real, not that one excerpt supports every part of a compound claim. If
`verified` is populated but `answerable` is empty, the runtime must abstain or
narrow the answer. Check `support.claim_support` and use `get` or `explain`
with the returned commit and line range before making a claim marked
`partial` or `unknown`.

The standalone runtime has a dependency-free local vector projection enabled
by default. Doctor reports `provider=builtin`,
`model=builtin-char-ngram-v1`, and `retrieval_mode=local-hybrid`; this is a
real local vector index, not a claim that a neural MiniLM model is installed.
`scope=all` returns repository and standalone-memory lanes in separate groups.
No black-box cross-backend RRF is used.

### Optional local neural retrieval

The default projection is intentionally small and offline, but it is not a
semantic language model. Hosts that need paraphrase and multilingual technical
term recall can explicitly configure a Hugging Face SentenceTransformers
model:

```bash
repository-memory semantic status --json
repository-memory semantic configure \
  --model <hugging-face-model-id> --download --json
repository-memory sync --all --json
```

The model is downloaded only by the explicit `semantic configure --download`
operation. A normal `sync` may build an index from an already-cached model but
never grants itself network/download permission. The repository vector cache is
derived user data keyed by source commit and model;
the Git source, line citation, exact/path routing, and negative-query policy
remain authoritative. If dependencies or model files are missing, doctor and
search report `lexical-fallback` rather than claiming hybrid retrieval.

The MCP installer records the verified Python runtime that loaded the model.
This matters on hosts with multiple Python installations: a model installed in
one interpreter is not silently assumed to be available to another MCP
process.

## Shared Team Memory

The explicit Team Memory tools are:

```text
memory_context     # task-start hydration
memory_publish     # explicit decision/failure/solution/handoff write
memory_feedback    # helpful/not_helpful/stale/wrong reuse feedback
memory_supersede   # explicit correction and lifecycle transition
memory_team_activate # explicit candidate review -> active
```

The read-only MCP tools also include `memory_timeline` for the ordered trace
view described above.

Candidate review is explicit. `repository-memory supervise` uses a
user-configured JSON argv command when one is available; without it the result
is `hold` and no record is activated. This prevents an endpoint or a
self-reported model answer from being mistaken for supervision. `--apply` is
the only mode that changes the user-level memory store; it never changes the
Git source.

The same evaluator is exposed as a benchmark entry point. It never downloads
external datasets:

```bash
repository-memory benchmark --suite public --json
repository-memory benchmark --suite public --root /path/to/knowledge --json
repository-memory benchmark --suite locomo --data /path/to/manifest.json --root /path/to/knowledge --json
```

When the command is run from a checked-out public repository, the first form
automatically discovers that repository as the fixture root. Use `--root` for
an explicit fixture or an RLVR profile.

External suites must provide a manifest or `queries.jsonl` plus `qrels.jsonl`;
an unsupported format is reported as such rather than producing fabricated
scores.

### Agent Memory Leaderboard Add/Search wrapper

The standalone core also ships a dependency-free synchronous HTTP wrapper for
the official Agent Memory Leaderboard contract. It implements `/health`,
`/add`, and `/search`, enforces `user_id` isolation, and persists each Add
before returning HTTP 200. See
[`docs/agent-memory-leaderboard.md`](docs/agent-memory-leaderboard.md) for the
Docker submission path. Local fixture results are not leaderboard results;
the official platform must run and review the submitted version.

```bash
repository-memory-aml --host 0.0.0.0 --port 8080
```

Records use the lifecycle `candidate -> active -> superseded|stale` and are
stored in a user-level SQLite cache. SQLite is the default backend behind a
`TeamMemoryBackend` seam; it uses WAL, a bounded busy timeout, and retryable
transactions for concurrent local agents. Records contain `type`, `scope`,
`provenance`, `confidence`, `author_agent`, validity windows, and reuse
feedback plus causal `revision`, `origin_node`, and `parent_revision` fields.
Reviews are recorded separately as `reviewed_by`/`activated_at`; they never
overwrite `author_agent`. Bundle schema 3 carries an append-only revision log,
so a receiver can fast-forward from an older known ancestor even when it
missed intermediate exports. Concurrent branches are reported as conflicts
instead of being resolved by wall-clock time. They are not a second Git
repository and are never written into the
canonical source tree.

For cross-machine or container handoff, use an explicit portable bundle:

```bash
repository-memory team-export --output /tmp/team-memory.json --json
repository-memory team-import --input /tmp/team-memory.json --json
```

The bundle merge is idempotent and reports inserts, updates, conflicts, and
feedback additions. This is explicit file-based synchronization; the public
runtime does not pretend to provide a hosted Team Memory service.

## Four memory layers

The bundled standalone memory runtime keeps conversation memory distinct from
repository evidence:

| Layer | Meaning | Default write policy |
| --- | --- | --- |
| L0 | Raw conversation/message | Explicit ingest or opt-in host capture; read-back required |
| L1 | Atomic fact extracted from conversation | Pending until extraction/read-back is observed |
| L2 | Scenario or generated long-term context | Candidate/pending until review |
| L3 | Stable profile/core memory | Explicit promotion and read-back only |

An API being reachable is not the same as having useful data. Doctor reports
capability, reachability, record counts, pending candidates, and read-back
verification separately. Every `memory.layers.L*` entry uses the same four-way
contract: `capability`, `api_status`, `population`, and `readback`. Population
is only `present` when the layer's actual query/read response returns records
or content; unsupported, unreachable, malformed, or unprobed states remain
`unknown` rather than being inferred from global health.

The repository also bundles a clean source snapshot of upstream MemoryCore and
MemoryKnowledge components as implementation references. They are not needed
by the default executable path. The standalone runtime owns the public JSON
contract, citations, SQLite state, and L0-L3 lifecycle; the vendor service is
only an explicit compatibility mode for users who choose to run it.

No live endpoint, model, provider, or credential is needed for the default
runtime. If an external mode is explicitly enabled, its endpoint and
credentials are discovered from user configuration or environment at runtime
and never committed to Git. `doctor --json` reports whether the actual backend
is `standalone-memory` or an explicitly selected external runtime.

## Optional local Memmy provider

This project can also call an already-installed local Memmy service through a
small adapter. We reuse its SQLite/FTS5 storage, native local-vector search,
layer-aware results, idempotent jobs, and existing panel; Memmy remains an
optional provider and is not copied into or made canonical by this repository.

```bash
repository-memory memmy status --json
repository-memory memmy configure --endpoint <memmy-endpoint> --json
repository-memory search "the user's question" --scope memory --json
repository-memory gui --json
```

The runtime reports compatibility providers separately. The built-in
standalone vector lane remains the default; an already-installed Memmy service
is not required and is never used to make the standalone doctor green. See
[memory-providers](docs/memory-providers.md) for the optional compatibility
contract.

## TencentDB MemoryKnowledge (Wiki / CodeGraph)

MemoryCore and MemoryKnowledge are different services:

- MemoryCore stores and recalls L0–L3 conversation memory.
- MemoryKnowledge builds Wiki pages and CodeGraph indexes from repository
  content.
- The repository citation index remains the default fact/evaluation path. A
  MemoryKnowledge result without a matching Git path, commit, line range and
  excerpt is returned as a candidate, never silently promoted to verified.

The Knowledge service is optional and can be enabled per installation:

```bash
repository-memory knowledge configure --json
repository-memory knowledge install
repository-memory knowledge status --json
repository-memory knowledge create --name project-docs --json
repository-memory knowledge sync --wiki-id <wiki-id> --json
repository-memory knowledge search --wiki-id <wiki-id> --query "..." --json
```

`knowledge sync` writes only the derived Wiki index. It filters hidden files,
secrets, binaries, oversized files and operational output directories; it does
not commit, push, pull, or rewrite the source repository. If the Knowledge
service is not configured or reachable, `doctor` reports `not_configured` or
`unreachable` while repository citation search and MemoryCore continue to work.

## MCP

The server uses local stdio and advertises the modern MCP discovery/metadata
path first, while retaining a small compatibility handshake for hosts that
have not migrated yet. The public tool list is intentionally limited to
read/diagnostic operations:

```text
memory_doctor
memory_sync
memory_search
memory_get
memory_timeline
```

Use the CLI for `init`, `source add`, `ingest-session`, `memory project`,
`feedback`, Team Memory publish/activate, and `memory promote-l3`. The MCP and CLI share the same
runtime and return the same read/query contract; the server is not bound to a
port.

## OpenClaw capture and guard

The OpenClaw extension is optional. It can:

1. observe whether project-fact turns use the repository-memory MCP route;
2. audit the bare built-in memory tool and direct-file fallback without
   blocking normal tool use;
3. audit tool metadata without storing full prompts or answers;
4. capture bounded user/assistant text after a completed turn into L0;
5. leave L2 as a reviewable candidate and never write L3 automatically.

Normal coding, testing, build, shell, Git-status, and patch tasks remain free to use
the host's normal tools. A host without
tool lifecycle hooks can still use the Skill/MCP contract, but cannot claim
that direct-file access is technically blocked.

## Public boundary

This project contains generic runtime code, fixtures, and documentation only.
It intentionally does not contain private repositories, organization-specific
evaluation sets, credentials, model names, internal hostnames, or user data.
Use `init`/`source add` to attach the repositories that are appropriate
for your own environment.

That boundary is enforced, not just asserted. `tools/scan-tree.sh` scans every
tracked file for credential blobs, credentials embedded in URLs, private
hostnames, and non-placeholder email addresses, and runs on every push. It
proves itself before reporting: each rule must match a planted sample, and
permitted placeholders must stay exempt, or the script fails instead of
reporting a clean tree. There is no exemption for `tests/` — test fixtures are
where an identifier is least likely to be noticed and most likely to survive.

Strings that cannot be expressed as a shape — a specific person's name, a
codename — go in `tools/banned.local.json`, which is gitignored and optional.
It is deliberately not committed: a checked-in list of the exact strings you are
hiding publishes every one of them to anyone who opens the file.

If you are upgrading an installation that used an earlier name for this skill,
the names to migrate away from are supplied by that deployment rather than
hardcoded here, since only the machine that ran the older install knows them:

| Variable | Effect |
| --- | --- |
| `REPOSITORY_MEMORY_LEGACY_SKILL_NAMES` | Comma-separated skill names to deregister from agents during install. |
| `REPOSITORY_MEMORY_LEGACY_OPENCLAW_PLUGIN_IDS` | Comma-separated OpenClaw plugin ids to disable during install. |
| `REPOSITORY_MEMORY_LEGACY_LABELS` | Comma-separated launchd labels of a previously installed service to remove. |

All three default to empty, which is correct for a fresh install.

See [docs/quickstart.md](docs/quickstart.md),
[docs/architecture.md](docs/architecture.md), and
[docs/troubleshooting.md](docs/troubleshooting.md).

Project maintenance and release boundaries are documented in
[CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
[CHANGELOG.md](CHANGELOG.md), and
[docs/compatibility.md](docs/compatibility.md). The privacy-free public
retrieval regression set lives in [eval/public](eval/public/README.md).

## License

MIT. See [LICENSE](LICENSE).
