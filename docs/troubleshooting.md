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

This is normal. The runtime remains usable with structured and lexical routing.
It must be described as `lexical`, not as semantic or hybrid. Configure a
provider in user config only if the environment can support it; the public Skill
does not hard-code one.

## MemoryCore is unreachable

Repository search does not depend on MemoryCore. Check `memorycore status` and
the endpoint/configuration fields in doctor. Do not report L0-L3 as populated
because an API is reachable. If there is no native backend, explicit ingest can
use the local fallback and will report its actual supported layers.

## OpenClaw still reads files directly

The guard is host-dependent. Confirm the profile-local extension is in the
profile's own `extensions/` directory, that the plugin is allowed, and that
`guardEnabled=true`. The guard only blocks direct tools for turns classified as
repository-fact turns; ordinary coding turns remain unaffected. A host without
`before_tool_call` cannot provide a hard block.

## The bare `memory_search` fails

That may be a host's unrelated built-in memory backend. Register and use the
namespaced Repository Memory MCP. The correct sequence is:

```text
memory_doctor -> memory_search -> memory_get
```

If the namespaced query has no verified citation, abstain. Do not fall back to
`read`, `exec`, grep, or a direct file scan while claiming the answer came from
Repository Memory.

## MCP client compatibility

The server advertises the modern protocol path and accepts framed stdio
messages. It retains a compatibility `initialize` path for older clients. If
a client rejects discovery, update its MCP integration or use the bundled CLI
until the host supports the modern metadata request.
