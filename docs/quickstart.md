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
    "semantic_available": false
  }
}
```

The exact adapter and counts are environment-specific. `supported_layers`
means the adapter knows how to inspect those layers; it does not mean every
layer contains accepted records.

## 3. Ask a question

Send the user's original question unchanged:

```bash
repository-memory search "What changed in the evaluation pipeline recently?" \
  --scope repository --json
```

If a result is verified, call `get` with its id and use the returned citation.
If all results are candidates or the answer is negative/unsupported, return an
explicit abstention. Do not turn the question into a filename and do not read
the source directly to bypass a failed search.

## 4. Add another source

```bash
repository-memory source add --path /path/to/another-repo --id another-repo --json
repository-memory sync --all --json
```

Source IDs are local stable handles. They do not need to match a remote name.

## 5. Use conversation memory explicitly

```bash
repository-memory search "What did we decide about retries?" \
  --scope memory --json
repository-memory ingest-session --input examples/session.json --json
```

Ingestion is a write. It must be requested explicitly. It reports the actual
L0/L1 read-back status and never silently creates an accepted L2 or L3 record.

## 6. MCP smoke test

```bash
repository-memory mcp
```

The process speaks stdio. A host should register the process, then call
`memory_doctor`, `memory_search`, and `memory_get`. The host namespace may
prefix these names; use the registered namespace, not a bare unrelated
`memory_search` tool.
