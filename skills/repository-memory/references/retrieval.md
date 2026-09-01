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

The default runtime searches an in-process SQLite store across L0 conversations,
L1 atomic records, L2 scenarios, and the L3 profile. It also maintains a
dependency-free `builtin-char-ngram-v1` local vector projection; when that
projection is available, diagnostics report `retrieval_mode=local-hybrid` and
`semantic_available=true` for the local capability. This is a local vector
projection, not a claim that a neural embedding provider is configured.
Explicit session ingestion persists L0/L1 and verifies them by read-back, then
creates an unaccepted L2 candidate. Explicit review accepts L2; L3 is written
only by explicit promotion and is verified by a second read. A vendor
MemoryCore endpoint is an optional compatibility backend and is never required
for the standalone path.

By default, the runtime fetches a remote reference and indexes a disposable snapshot without changing the working tree. Use `--local` only when the current checkout is intentionally the source. An explicitly configured `local_only` source is an offline/local snapshot contract: it is fresh relative to its recorded commit when clean, but never implies that the commit is the latest remote revision. A dirty local source is not fresh and its results must not silently be presented as remote facts.

When fetch or an adapter fails, the runtime may use conservative local exact evidence. Such fallback results must identify the fallback in diagnostics and cannot be upgraded merely because they are textually similar.

Repository semantic state is a derived, same-source cache. Documents that fit
the encoder bound remain one chunk; long dated Markdown is split at stable date
sections and then bounded by size, with physical line locators retained. Chunk
digests let a compatible cache from the previous commit supply vectors for
unchanged sections, while a normal large-repository search encodes only a
bounded changed set. A source with no compatible prior cache still requires an
explicit sync before semantic retrieval is advertised.

For frequent CLI or hook recall, a configured Hugging Face model can run as a
resident loopback service. This changes only the transport: provider, model,
dimension, persisted vectors, lexical evidence guards, and citation rules stay
the same. If the resident process is unavailable, the runtime may load the
cached model in-process and must report that transport explicitly.

Natural-language CJK questions are handled inside the runtime. The lexical
fallback keeps normal path/ASCII tokens, adds short CJK fragments for entity
matching, and routes personal temporal questions such as “某人最近在做什么”
to source layers named `standup`, `daily`, `diary`, or `journal` when present.
Inside a multi-date personal file, an unbounded recent/latest request selects
the newest dated Markdown section before choosing its citation window; an
explicit date request still selects the named historical section.
This is query parsing, not semantic retrieval: `diagnostics.retrieval_mode`
remains `lexical` and the response must still carry a valid citation.

Agents should send the user's question as-is. A filename-shaped second query
is not required and should not be used to hide a failed first retrieval.

`memory_search` defaults to `scope="auto"`, the multi-plane task-start path.
Repository, conversation memory, and Team Memory lanes run in their own
rankings and come back as separate groups; no cross-backend score is invented.
The top-level answer surface stays repository-only, so `retrieval_mode`
describes how that answer was retrieved and never borrows the memory lane's
`local-hybrid` strategy to describe a lexically-found repository result.
