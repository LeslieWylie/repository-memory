# Git-backed Team Memory

Repository Memory has three deliberate planes:

```text
Git repository evidence       source-backed facts and citations
local MemOS/SQLite memory     private L0/L1 runtime context
Git Team Memory                reviewed L1/L2/L3 knowledge shared by agents
```

The third plane closes the multi-agent loop without copying raw chats into a
canonical repository.

## Configure one agent

```bash
repository-memory team-configure \
  --repository /path/to/team-knowledge-data \
  --agent-id yaole --json
```

The command writes only user configuration. It does not modify the repository.
The repository must contain `knowledge/team-memory/README.md`.

## Capture and sync

When `auto_sync` is enabled, an OpenClaw `agent_end` turn follows this path:

```text
bounded turn
  -> local L0 write/read-back
  -> local L1 extraction/read-back
  -> local Team Memory candidate
  -> team-memory/inbox/<agent>/candidate.md
```

The hook does not commit, push, activate, or promote. The explicit close of
that gap is one command — rebase-pull the team repository, run the same
team-sync, commit only what it wrote under `knowledge/`, and push:

```bash
repository-memory team-publish --json
repository-memory team-status --json
```

Schedule it per node (`--no-push`/`--no-pull` narrow it when needed):

```text
41 19 * * * repository-memory team-publish --json >> ~/team-publish.log 2>&1
```

`team-sync` remains available on its own and is idempotent. A lifecycle
transition from inbox candidate to active/accepted is represented as a Git
move when the existing file belongs to the managed inbox. Conflicting files
are written under `knowledge/team-memory/conflicts/` and are never
overwritten. Review is deliberately not part of publish: activation stays an
explicit supervised step.

## Review and reuse

Configure a supervisor command through user configuration or
`REPOSITORY_MEMORY_SUPERVISOR_COMMAND`. It receives one JSON object on stdin
and must return:

```json
{
  "decision": "accept|hold|reject",
  "confidence": 0.0,
  "model": "runtime-provided-model",
  "reason": "...",
  "unsupported_claims": []
}
```

Only an explicit `--apply` can activate a candidate. The reviewer must check
secrets, reusable content, provenance, citation and freshness. L3 still needs
an explicit promotion/accept operation.

```bash
repository-memory supervise --lane team --apply --json
repository-memory team-sync --json
```

Every other agent runs the same `team-sync` and then searches the shared
active/accepted plane. Results stay grouped:

```text
repository evidence | team memory | local memory
```

No cross-plane score fusion is used. Team memory never becomes a Git citation
unless its own repository evidence is separately validated.

## Safety boundary

- L0 raw conversations remain local.
- Secrets and tool-only messages are removed before candidate export.
- Candidates are visible in the inbox but not in default verified search.
- Search/sync never commits or pushes.
- Existing files are never deleted; managed lifecycle moves and conflicts are
  reported in the JSON receipt.
- The canonical Git repository remains the recoverable source of truth.
