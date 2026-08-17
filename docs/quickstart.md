# Quickstart

## 1. Install

```bash
python3 install.py --target auto --source-root /path/to/your-repo --json
```

For a host with a profile:

```bash
python3 install.py --target openclaw \
  --openclaw-config /path/to/openclaw.json \
  --openclaw-agent <agent-id> \
  --source-root /path/to/your-repo --json
```

The OpenClaw installer requires an explicit agent selection and only changes
that agent's Skill/tool permissions. Repeat `--openclaw-agent` for a small
allowlist, or use `--openclaw-all-agents` only when every configured agent is
intentionally in scope.

The source root must be a Git repository or a directory explicitly intended
as a knowledge source. The installer writes user config and derived cache;
the source files stay untouched.

### Remote host one-liner

On a remote OpenClaw host, send the bot this exact command (replace only the
knowledge Git URL):

```bash
curl -fsSL https://raw.githubusercontent.com/LeslieWylie/repository-memory/main/bootstrap.sh | sh -s -- --target openclaw --openclaw-agent auto --source-url <knowledge-git-url> --source-branch main --json
```

The command clones the installer into user cache, discovers the active agent,
registers the namespaced stdio MCP, enables the advisory lifecycle extension,
registers the knowledge source, and runs `doctor` plus the MCP smoke probe.
It does not modify the knowledge checkout, commit/push anything, or expose
feedback/promote tools. For a multi-agent profile use
`--openclaw-all-agents` explicitly.

The install is self-contained. Do not start a vendor MemoryCore, Memmy, Wiki,
or embedding service. The first `doctor` creates the user-level SQLite state
and reports `memory.backend=standalone-memory`, `external_dependency=false`,
and `embedding.strategy=local-hybrid` with the built-in vector projection.

## 2. Inspect readiness

```bash
repository-memory doctor --json
```

Look for:

```json
{
  "repository": {"status": "ready"},
  "routing": {"repository_mcp": "ready"},
  "memory": {
    "supported_layers": ["L0", "L1", "L2", "L3"],
    "layers": {
      "L0": {
        "capability": "supported",
        "api_status": "ready",
        "population": "empty",
        "readback": "verified"
      }
    }
  }
}
```

The exact adapter and counts are environment-specific. `supported_layers`
means the adapter knows how to inspect those layers; it does not mean every
layer contains records. Read each `layers.L*` object as four independent facts:
`capability` says whether the adapter implements the layer, `api_status` says
whether that layer API is usable, `population` says whether the response proves
records are present or empty, and `readback` records whether the probe was
verified, explicitly pending, or unknown. Never infer `population=present`
from `supported_layers`, global `reachable`, or `api_status=ready`.

## 3. Hydrate a multi-agent task

For coding work, start with the shared context package when previous agent
work may matter:

```bash
repository-memory context "What changed in the evaluation pipeline recently?" --json
```

The response keeps Git evidence separate from Team Memory decisions, failures,
solutions, discoveries, and handoffs. `team_candidates` are not accepted facts.
The context response uses `retrieval_mode=multi-source-lexical`: the two
recall lanes are parallel, but their scores and provenance are not mixed.

## 4. Ask a repository-only question

Send the user's original question unchanged:

```bash
repository-memory search "What changed in the evaluation pipeline recently?" \
  --scope repository --json
```

If a result is verified, call `get` with its id and use the returned citation.
If all results are candidates or the answer is negative/unsupported, return an
explicit abstention. Do not turn the question into a filename and do not read
the source directly to bypass a failed search.

## 5. Publish reusable team knowledge explicitly

```bash
repository-memory publish --input memory.json --status candidate --json
repository-memory team-activate --id team:decision:<id> --reviewer reviewer-1 --json
repository-memory feedback team:decision:<id> --rating helpful --note "reused" --json
repository-memory team-export --output /tmp/team-memory.json --json
repository-memory team-import --input /tmp/team-memory.json --json
```

Use `type=decision|failure|discovery|solution|handoff`, include scope and
provenance, and let a reviewer promote candidates to `active` or supersede an
outdated record.

## 6. Add another source

```bash
repository-memory source add --path /path/to/another-repo --id another-repo --json
repository-memory sync --all --json
```

Source IDs are local stable handles. They do not need to match a remote name.

## 7. Use conversation memory explicitly

```bash
repository-memory search "What did we decide about retries?" \
  --scope memory --json
repository-memory ingest-session --input examples/session.json --json
repository-memory memory project --json
```

Ingestion is a write. It must be requested explicitly. It reports the actual
L0/L1 read-back status. `memory project` turns existing sessions into reviewable
L2 candidates. The standalone runtime also supports explicitly accepted L2/L3
records; it never silently accepts an L2 or writes L3.

## 8. MCP smoke test

```bash
repository-memory mcp
```

The process speaks stdio. A host should register the process, then call the
four public tools `memory_doctor`, `memory_sync`, `memory_search`, and
`memory_get`. The host namespace may prefix these names; use the registered
repository-memory namespace, not a bare unrelated `memory_search` tool.

The public MCP is intentionally read-only. Use the CLI for `init`,
`source add`, `ingest-session`, feedback, review, promotion, and Team Memory
publishing. CLI and MCP share the same user-level runtime and return the same
query contract.
