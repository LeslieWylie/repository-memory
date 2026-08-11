# Retrieval and freshness

The runtime selects a mode from the request and keeps sources separate. It does not apply a black-box score fusion across adapters.

Scope is explicit. `repository` is the default and searches only canonical
repository evidence; it must not call the conversation-memory APIs.
`memory` searches the configured conversation/atomic plane and may read higher
layers for context. `all` runs both paths and returns separate groups. Raw L0
conversation is an independent memory result; it may carry `linked_evidence`,
but it is never rewritten as a Git document.

- `exact`: identifiers, names, paths, or explicit terms. Prefer direct records and source lines.
- `semantic`: topical or rephrased requests. Treat lexical/entity expansion as a fallback unless diagnostics explicitly report semantic capability.
- `temporal`: latest, recent, date, or report requests. Confirm the date and commit in the citation.
- `cross-source`: questions connecting entities or repositories. Verify each relation independently.
- `deep`: explicit investigation mode that can include otherwise excluded raw areas.
- `negative`: abstain unless the source contains direct evidence for the negative claim.

If an adapter reports a conversation-memory plane, its L0-L3 recall is kept as
memory results and is not fused with repository scores. Use the returned layer
and query-source fields to explain provenance. If the memory plane is configured
but not reachable, report that state; repository scope remains usable.

The native MemoryCore adapter searches L0 conversations and L1 atomic records,
and reads L2 scenarios and the L3 profile through their layer-specific APIs.
Explicit session ingestion uses the durable conversation mutation path so the
records remain visible to subsequent L0/L1 queries; a batch seed command that
only creates a disposable workspace is not treated as successful persistence.
When no native backend is configured, the Skill uses a user-level SQLite
fallback for explicit session ingestion and lexical L0/L1 recall. Its doctor
output marks L2/L3 unsupported rather than pretending deterministic storage is
equivalent to summarisation or profile memory.

By default, the runtime fetches a remote reference and indexes a disposable snapshot without changing the working tree. Use `--local` only when the current checkout is intentionally the source. An explicitly configured `local_only` source is an offline/local snapshot contract: it is fresh relative to its recorded commit when clean, but never implies that the commit is the latest remote revision. A dirty local source is not fresh and its results must not silently be presented as remote facts.

When fetch or an adapter fails, the runtime may use conservative local exact evidence. Such fallback results must identify the fallback in diagnostics and cannot be upgraded merely because they are textually similar.

Natural-language CJK questions are handled inside the runtime. The lexical
fallback keeps normal path/ASCII tokens, adds short CJK fragments for entity
matching, and routes personal temporal questions such as “某人最近在做什么”
to source layers named `standup`, `daily`, `diary`, or `journal` when present.
This is query parsing, not semantic retrieval: `diagnostics.retrieval_mode`
remains `lexical` and the response must still carry a valid citation.

Agents should send the user's question as-is. A filename-shaped second query
is not required and should not be used to hide a failed first retrieval.

`memory_context` is the multi-source task-start path. Its repository and Team
Memory lanes run in parallel, then return sectioned provenance. Its
`retrieval_mode` is `multi-source-lexical` until a configured semantic backend
actually exists; do not call it hybrid merely because two lexical sources are
present.
