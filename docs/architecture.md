# Architecture

## Boundaries

Repository Memory has four deliberately separate boundaries:

1. **Canonical sources** are Git repositories or explicitly configured
   document roots. They remain user-owned and are never rewritten by search.
2. **Derived repository indexes** are disposable local cache data. A source
   commit, path, line range, and excerpt are stored with each result.
3. **Conversation memory** is an optional adapter. It preserves L0/L1/L2/L3
   layer identity and never becomes repository evidence.
4. **Shared Team Memory** is a user-level derived SQLite plane for reusable
   decisions, failures, discoveries, solutions, and handoffs. Its experience
   provenance is separate from Git citations and its lifecycle supports
   candidate, active, stale, and superseded states.
5. **Host integration** is a thin MCP registration and optional lifecycle
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

Task context path:

```text
memory_context
  -> parallel repository and Team Memory recall
  -> sectioned lexical context package
  -> agent
```

The source adapter ranks within its own source. `memory_context` is the fusion
seam, but it returns repository evidence, decisions, failures, solutions,
discoveries, and handoffs as separate sections. The two recalls run in
parallel, but scores are not combined into an opaque global RRF score. The
accurate capability name is `multi-source-lexical`, not semantic hybrid.

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

Shared Team Memory follows a separate state machine:

```text
explicit publish or bounded task-end extraction
  -> candidate
  -> explicit review/active
  -> feedback and reuse ranking
  -> explicit supersede or stale transition
```

The default Team Memory backend is a local SQLite adapter behind the
`TeamMemoryBackend` interface. It enables same-host sharing and uses WAL,
busy-timeout, and bounded transaction retries. Each record carries a causal
`revision`, `origin_node`, and `parent_revision`; bundle import applies only a
matching child revision and reports concurrent branches. Cross-host/container
transfer is an explicit JSON bundle export/import operation; it is not silently
called remote sync and no hosted service is claimed by the core runtime.

## MCP transport

The server is a stdio process. It accepts the modern metadata/discovery path
and framed messages, and retains newline JSON plus the compatibility
`initialize` handshake for older clients. Each request is independently
validated. Tool calls dispatch directly to the same functions used by the CLI.

## Security and privacy

Credentials are read at runtime from user configuration, environment, or an
OS secret store. They are not written to source indexes, MCP responses, audit
records, or Git. The audit proxy records tool metadata, counts, freshness, and
latency, not full query/answer bodies. The OpenClaw guard is advisory/output
validation and does not block normal file, shell, Git, test, or debugging tools.
