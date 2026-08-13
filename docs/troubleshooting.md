# Troubleshooting

## `not_installed`

The host has neither the Skill nor the MCP registration. Run the installer for
that host and start a new agent turn. A config file alone is not proof that the
server can start.

## `repository: not_configured`

No source was registered. Ask the operator for the intended Git root, then run:

```bash
repository-memory init --path /path/to/repo --json
```

The runtime intentionally does not register an arbitrary current directory just
to make doctor green.

## `stale`, `dirty`, or `fallback`

Run `repository-memory sync --json`. A remote snapshot is preferred. If fetch
fails, inspect `fetch_error`; the result is still usable only as an explicitly
labelled local fallback. Use `--local` only when local uncommitted state is
intended.

## `semantic_available: false`

The default standalone runtime should report the built-in local vector lane as
`embedding.available=true` and `strategy=local-hybrid`. If it reports false,
the process is using an explicitly selected compatibility backend or an older
installed package. Reinstall the current project and rerun `doctor`; do not
claim semantic retrieval from a keyword-only compatibility response.

## MemoryCore is unreachable

Repository search does not depend on MemoryCore. Check `memorycore status` and
the endpoint/configuration fields in doctor. Do not report L0-L3 as populated
because an API is reachable. If there is no native backend, explicit ingest can
use the local fallback and will report its actual supported layers.

## OpenClaw still reads files directly

The extension is advisory and host-dependent. It records explicit file reads
and high-confidence source-reading commands as routing observations; ordinary
`exec`, tests, builds, shell, Git, and patch operations remain available.
Confirm the profile-local extension is in the
profile's own `extensions/` directory, that the plugin is allowed, and that
`guardEnabled=true`. The default `enforcement=audit` records direct fallback
without blocking it. The old `enforcement=enforce` setting is retained for
configuration compatibility but does not turn this extension into a shell
sandbox. A host without `before_tool_call` can still use the MCP contract, but
cannot provide routing audit receipts.

If only one OpenClaw agent should use Repository Memory, install with
`--openclaw-agent <id>`. The installer no longer silently writes the Skill and
MCP permissions to every configured agent; use `--openclaw-all-agents` only
when that broad scope is intentional.

## The bare `memory_search` fails

That may be a host's unrelated built-in memory backend. Register and use the
namespaced Repository Memory MCP. The correct sequence is:

```text
memory_doctor -> memory_search -> memory_get
```

If the namespaced query has no verified citation, mark the repository claim
unknown. Direct workspace inspection is allowed for a coding task, but do not
claim it came from Repository Memory.

## MCP client compatibility

The server advertises the modern protocol path and accepts framed stdio
messages. It retains a compatibility `initialize` path for older clients. If
a client rejects discovery, update its MCP integration or use the bundled CLI
until the host supports the modern metadata request.
