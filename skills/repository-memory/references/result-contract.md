# Result contract

Search responses use this shape:

```json
{
  "schema_version": 4,
  "query": "...",
  "mode": "exact|semantic|temporal|cross-source|deep",
  "scope": "repository|memory|all",
  "verified": [],
  "candidates": [],
  "groups": {},
  "abstain": false,
  "freshness": {},
  "diagnostics": {}
}
```

Repository results expose a stable id, source/repository, commit, commit type,
path, line range, excerpt, evidence status, generated status, freshness, and
citation metadata. Every result may also expose `support` with
`matched_terms`, `unmatched_terms`, `coverage`, `claim_support`, and
`supporting_spans`. Native or local memory results replace the Git locator with
a layer, stable memory id, locator/evidence, and backend freshness; they must
not fabricate a repository path or commit. A `local-memory` result is a
deterministic L0/L1 fallback, not evidence that L2/L3 summarisation or profile
memory exists.

Results recalled from a conversation-memory adapter may also expose `memory`
metadata with `layer` (for example `L0` through `L3`), `type`, `query_source`,
and `strategy`. These fields describe where the adapter found the item; they do
not bypass citation validation or upgrade a candidate to `verified`.

`support` is a lexical/structural diagnostic, not semantic entailment. A
`direct` value means the inspected evidence window contains the extracted
query terms; it does not prove relation direction, negation, or every part of
a compound claim. Use `supporting_spans` and `get`/`explain` before asserting
claims whose meaning depends on those distinctions.

`verified` is document-level: the runtime resolved the cited path, commit, line
range, and excerpt, or a native memory backend returned a stable layer/id/evidence
tuple with no disqualifying status. It does not mean every claim in the query
is supported by one excerpt. `candidates` contains incomplete, stale,
generated, inferred, pending, or merely related material. Agents must not
silently promote candidates to facts.

For `scope=all`, `groups.repository` and `groups.memory` are independent result
sets. Top-level `verified`/`candidates` are intentionally empty; consume the
group matching the claim type. The default `scope=repository` keeps the
backwards-compatible top-level aliases.

`results` may appear as a backwards-compatible alias for `verified`; new callers should use `verified` and `candidates`.

MemoryCore can be used without any repository source. In that case `doctor`
reports `repository.status=not_configured`, while `scope=memory` still returns
the native L0-L3 readiness and results. A repository source must be explicitly
initialized before `scope=repository` can return document evidence.
