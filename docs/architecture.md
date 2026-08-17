# Architecture

## Boundaries

Repository Memory has four deliberately separate boundaries:

1. **Canonical sources** are Git repositories or explicitly configured
   document roots. They remain user-owned and are never rewritten by search.
2. **Derived repository indexes** are disposable local cache data. A source
   commit, path, line range, and excerpt are stored with each result.
3. **Conversation memory** is an in-process SQLite L0-L3 runtime by default.
   It preserves layer identity and never becomes repository evidence. A vendor
   MemoryCore endpoint is an explicit compatibility option, not a prerequisite.
4. **Shared Team Memory** is a user-level derived SQLite plane for reusable
   decisions, failures, discoveries, solutions, and handoffs. Its experience
   provenance is separate from Git citations and its lifecycle supports
   candidate, active, stale, and superseded states.
5. **Host integration** is a thin MCP registration and optional lifecycle
   extension. It does not contain a second ranking or storage implementation.
6. **Vendored upstream components** are a pinned source snapshot inside the
   Skill package. They document and support the native L0-L3 lifecycle, but do
   not create a second public API, index, or score-fusion path.

## Request path

```text
MCP / CLI
  -> doctor and source discovery
  -> scope router
       repository -> snapshot -> structured local index -> citation validator
       memory     -> standalone SQLite L0-L3 store (external MemoryCore is opt-in)
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
parallel, but scores are not combined into an opaque global RRF score. When a
repository query omits `source`, a small lexical/path anchor can order the
source buckets (for example a repository-specific filename); ties preserve
configured order. This is routing, not cross-source score fusion. The
accurate capability name is `multi-source-lexical`, not semantic hybrid.

## Freshness

For a source with a remote, sync fetches refs and builds an isolated detached
snapshot in user cache. It does not pull or alter the worktree. If fetching
fails, the runtime can expose a local fallback, but its freshness says
`fallback` and it cannot silently become fresh evidence. `--local` is an
explicit opt-in for current checkout state.

## Derived repository knowledge

The local index stores two conservative metadata layers in addition to document
text:

- date anchors found in the path, Markdown headings, and explicit date fields;
- explicit local Markdown/path references resolved to files in the same source.

Latest queries use the date anchors. Relationship queries may expand one hop
through the explicit references and return those files in `related`. This is a
small, deterministic and inspectable graph seam; it does not infer edges from
embedding similarity and it does not require a graph database. The canonical
source remains the file and its Git citation.

The current semantic capability is reported per source. `local-hybrid` means
the optional deterministic builtin projection is available; it does not mean a
neural model is installed. A neural provider and an LLM supervisor remain
explicit opt-ins. For large sources, the first pass deliberately uses the
lexical/path lane and defers projection loading until lexical retrieval has
no result; this keeps ordinary filename/date lookups responsive without
removing the semantic rescue path.

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
  -> L1 atomic projection [write + read-back]
  -> L2 scenario [candidate]
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
`revision`, `origin_node`, and `parent_revision`. Bundle schema 3 also carries
an append-only revision log, allowing a receiver to fast-forward from a known
ancestor after missed intermediate exports; concurrent branches are still
reported rather than selected. Review metadata is separate from authorship.
Cross-host/container transfer is an explicit JSON bundle export/import
operation; it is not silently called remote sync and no hosted service is
claimed by the core runtime.

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
### Scale behavior

Derived repository indexes record `document_count`, `text_bytes`,
`index_bytes`, and a `scale_class` (`small`, `medium`, or `large`). The query
path reuses these values instead of rescanning every document merely to decide
whether semantic projection should be deferred. Large sources therefore take
a lexical/path first pass; the optional semantic cache is built only when the
first pass needs rescue. The index remains disposable derived state and the
canonical checkout is never rewritten.
