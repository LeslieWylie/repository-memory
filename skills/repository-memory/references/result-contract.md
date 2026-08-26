# Result contract

Search responses use this shape:

```json
{
  "schema_version": 4,
  "query": "...",
  "mode": "exact|semantic|temporal|cross-source|deep",
  "scope": "auto|repository|memory|all",
  "verified": [],
  "answerable": [],
  "candidates": [],
  "groups": {},
  "abstain": false,
  "answered_by": [],
  "freshness": {},
  "diagnostics": {}
}
```

Repository results expose a stable id, source/repository, commit, commit type,
path, line range, excerpt, evidence status, generated status, freshness, and
citation metadata. Every result may also expose `support` with
`matched_terms`, `unmatched_terms`, `coverage`, `claim_support`, and
`supporting_spans`. Standalone or native-compatible memory results replace the
Git locator with a layer, stable memory id, locator/evidence, and backend
freshness; they must not fabricate a repository path or commit. The default
`standalone-memory` backend can represent L0-L3, but an L2 candidate is not an
accepted fact and L3 is only valid after explicit promotion plus read-back. A
legacy `local-memory` result may still be limited to L0/L1; its actual backend
and layer status must be reported rather than inferred.

Results recalled from a conversation-memory adapter may also expose `memory`
metadata with `layer` (for example `L0` through `L3`), `type`, `query_source`,
and `strategy`. These fields describe where the adapter found the item; they do
not bypass citation validation or upgrade a candidate to `verified`.

Standalone assistant turns may additionally expose `retrieval_keys`,
`context`, and `context_strategy=adjacent-session-turns`. A retrieval key is a
bounded preceding user question used to associate a concise answer with the
way it was asked; adjacent context stays inside the same session, layer, and
ingest/request batch. It is never factual evidence. A key-only match reports
`support.claim_support=associated` and `retrieval_key_is_evidence=false`, so it
may appear in `verified` as an investigation lead but cannot enter
`answerable` or suppress abstention.

`support` is a lexical/structural diagnostic, not semantic entailment. A
`direct` value means the inspected evidence window contains the extracted
query terms; it does not prove relation direction, negation, or every part of
a compound claim. Use `supporting_spans` and `get`/`explain` before asserting
claims whose meaning depends on those distinctions.

`verified` is document-level: the runtime resolved the cited path, commit, line
range, and excerpt, or a native memory backend returned a stable layer/id/evidence
tuple with no disqualifying status. It does not mean every claim in the query
is supported by one excerpt.

`answerable` is the safe answer surface. Repository evidence requires
`support.claim_support=direct`; a prior assistant answer may also be partial
when its own text independently matched the query. `associated` retrieval-key
matches are never answerable. `results` is an alias for `answerable`, not for
every verified document. A response may therefore contain
verified documents while still returning `abstain=true`; this means retrieval
found real citations but no returned excerpt supports the complete claim. Use
`get`/`explain` with the citation's commit and line range, or answer only the
directly supported subclaims.

`candidates` contains incomplete, stale, generated, inferred, pending, or
merely related material. Agents must not silently promote candidates to facts.

`citation.pinned` and `evidence_status=worktree` describe a citation whose
excerpt was read back and matched against the file on disk, but whose source
tree has uncommitted changes, so the excerpt cannot be pinned to a commit. Such
a result is verified and can be answerable: the excerpt is exactly what the
working tree says. It is not `stale` — stale means the cited commit is not the
expected one, so the evidence may no longer say what it said. When quoting an
unpinned citation, say the source is a working tree rather than a commit;
`citation.commit` is still reported and `commit_type` is `local_worktree`.

For `scope=all`, `groups.repository` and `groups.memory` are independent result
sets. Top-level `verified`/`candidates` are intentionally empty; consume the
group matching the claim type. The default `scope=repository` keeps the
backwards-compatible top-level aliases.

For the default `scope=auto`, the top-level surface is the repository plane —
byte-for-byte what `scope=repository` returns — and `groups` carries
`repository`, `memory`, and `team` separately. `abstain` therefore describes
the repository plane **only**. It deliberately does not weaken when
conversation memory or a team decision can answer: an uncited plane must never
suppress an abstention the evidence guards depend on.

`answered_by` closes the gap that creates. It lists the planes that produced
something answerable — any of `repository`, `memory`, `team` — and is `[]` when
nothing did, or `null` outside `auto`. A caller that reads `abstain` alone will
give up on a question whose answer sits in `groups.memory`; read `answered_by`
first, then answer from the named group, labelling non-repository material as
conversation memory or a team decision rather than as a Git citation. Only
accepted team records (`groups.team.active`) count; `groups.team.candidates`
never do.

`results` is the safe answer alias for `answerable`; use `verified` only for
document retrieval diagnostics and evaluation.

MemoryCore can be used without any repository source. In that case `doctor`
reports `repository.status=not_configured`, while `scope=memory` still returns
the native L0-L3 readiness and results. A repository source must be explicitly
initialized before `scope=repository` can return document evidence.
