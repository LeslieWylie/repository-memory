# Evidence quality and attribution

A retrievable record is not necessarily an answer. Readback verifies the stored
text and its origin; it does not verify an assistant's assertion or establish
who performed the described work.

## Contract

- Repository citations keep commit, path and physical line coordinates.
  `evidence_date` describes the selected dated section, not file modification
  time. Latest queries select topic-bearing sections and withhold older answers
  when newer retrieved evidence does not establish the requested claim.
- Conversation questions, bare acknowledgements and simple abstentions cannot
  answer factual queries. Project identifiers remain required even when they
  touch Chinese text. Explicit personal subjects must agree with retrieved
  per-person source paths.
- `attribution.recorded_by` and `source_session` identify recording provenance.
  `work_performed_by` requires explicit provenance. Tool use, Git authorship and
  a recorder's name do not establish team ownership. The named-team guard
  currently recognizes Latin team names followed by `team` or `团队`; it is a
  conservative lexical guard, not a general entity-resolution model.
- Current-state queries with repository evidence retain conversation/team
  material as `historical_context`; it does not independently answer the current
  state. A partial latest citation can legitimately result in abstention.
- New capture preserves L0 and projects eligible statements into L1 with a
  source-record link. `extraction.fact_verified=false` makes clear these are
  statement traces, not independently verified atomic facts. Existing records
  remain readable; queries filter their eligibility without destructive cleanup.
- Exact Team Memory duplicates with the same scope, author, lifecycle status
  and validity share one search slot. `aliases` preserves every original ID.
  Different ownership or lifecycle states are not merged.
- Health exposes last capture time and duplicate/trace counts. Enabling a
  capture integration is separate from installing this retrieval change.

## Reference projects and decision (2026-09-07)

We reviewed the official repositories of
[Graphiti](https://github.com/getzep/graphiti),
[Mem0](https://github.com/mem0ai/mem0), and
[Hindsight](https://github.com/vectorize-io/hindsight).
They provide useful examples of temporal provenance, explicit memory processing
and separating retained experiences from claims about the world. Their GitHub
popularity is a discovery signal, not evidence of accuracy on this corpus.

The immediate defects are eligibility, attribution, span selection and stale
summary reuse. Keep Git as the canonical evidence store and SQLite as the local
memory store. Repair these boundaries before considering a graph database or
replacing the retrieval backend. This change borrows these design principles;
it does not implement any of those projects or claim equivalent capabilities.

## Validation

`test_evidence_quality.py` uses synthetic fixtures for attached identifiers,
question tails, refusal reuse, ownership, physical dates, topic-specific latest
sections, duplicate aliases, endpoint concepts and older-answer suppression.
Run the repository tests and public citation/team evaluation gates alongside
live search plus pinned readback on the installed runtime. Private corpus
examples and capture traces belong outside this public repository.
