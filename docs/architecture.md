# Architecture

## Boundaries

Repository Memory has four deliberately separate boundaries:

1. **Canonical sources** are Git repositories or explicitly configured
   document roots. They remain user-owned and are never rewritten by search.
2. **Derived repository indexes** are disposable local cache data. A source
   commit, path, line range, and excerpt are stored with each result.
3. **Conversation memory** is an optional adapter. It preserves L0/L1/L2/L3
   layer identity and never becomes repository evidence.
4. **Host integration** is a thin MCP registration and optional lifecycle
   extension. It does not contain a second ranking or storage implementation.

## Request path

```text
MCP / CLI
  -> doctor and source discovery
  -> scope router
       repository -> snapshot -> structured local index -> citation validator
       memory     -> native MemoryCore or local memory fallback
       all        -> both branches, returned as separate groups
  -> normalized verified/candidates contract
```

The source adapter ranks within its own source. Results from different sources
are kept in source groups or deterministically interleaved; scores are not
combined into an opaque global RRF score.

## Freshness

For a source with a remote, sync fetches refs and builds an isolated detached
snapshot in user cache. It does not pull or alter the worktree. If fetching
fails, the runtime can expose a local fallback, but its freshness says
`fallback` and it cannot silently become fresh evidence. `--local` is an
explicit opt-in for current checkout state.

## Evidence state

The citation validator checks:

- the path is inside the selected source;
- the commit matches the snapshot when a commit is supplied;
- line numbers are valid;
- the excerpt is present in the cited window;
- the file is not a secret, hidden, binary, or excluded generated area.

Document verification and claim support are separate. A valid document citation
can be `verified` while a compound question still has
`claim_support=partial`; the agent must limit its answer to the supported span.

## Memory state machine

```text
explicit ingest or opt-in capture
  -> L0 raw conversation [write + read-back]
  -> L1 atomic extraction [pending until observed]
  -> L2 scenario [candidate/pending]
  -> human review and explicit accept
  -> L3 profile/core [write + read-back]
```

An empty L2/L3 store is reported as empty or unsupported. An API endpoint being
reachable only proves capability, not accumulated quality.

## MCP transport

The server is a stdio process. It accepts the modern metadata/discovery path
and framed messages, and retains newline JSON plus the compatibility
`initialize` handshake for older clients. Each request is independently
validated. Tool calls dispatch directly to the same functions used by the CLI.

## Security and privacy

Credentials are read at runtime from user configuration, environment, or an
OS secret store. They are not written to source indexes, MCP responses, audit
records, or Git. The audit proxy records tool metadata, counts, freshness, and
latency, not full query/answer bodies. The OpenClaw guard blocks repository-fact
bypasses only when the host actually exposes the required lifecycle hooks.
