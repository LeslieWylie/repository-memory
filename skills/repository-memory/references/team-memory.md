# Shared Team Memory

Team Memory is the compact knowledge plane shared by multiple agents. It is
not a copy of every conversation and it is not a replacement for Git evidence.
Use it for information that another agent can reuse:

```text
evidence | decision | discovery | failure | solution | handoff
```

## Publish contract

`memory_publish` is an explicit write. A record should contain:

```json
{
  "type": "failure",
  "title": "Network reachability, not authentication, caused the download failure",
  "content": "...what was observed, attempted, and resolved...",
  "scope": {"repo": "example", "issue": "EX-42", "branch": "fix/network"},
  "provenance": {"agent": "coder", "session": "...", "commits": ["abc123"]},
  "confidence": 0.9
}
```

New records default to `candidate`. A caller may explicitly publish an
`active` record when the result has been reviewed or has sufficient direct
provenance. The runtime rejects secret-like content and records are stored in
the user-level data directory, not the canonical repository.

## Lifecycle

```text
candidate -> active -> superseded
                    \-> stale
```

`memory_supersede` publishes a replacement and marks the old record
`superseded`; search excludes superseded and stale records by default. This
prevents a later correction from competing with an obsolete decision.
`memory_team_activate` is the explicit review operation for a captured
candidate; ordinary capture never promotes it automatically.

`memory_feedback` accepts `helpful`, `not_helpful`, `stale`, or `wrong`. The
feedback affects reuse ranking and lowers confidence for stale/wrong reports.
`wrong` immediately marks an active record stale. Two stale reports from
different named agents mark an active record stale; one report only applies a
penalty. Expired `valid_until` records are excluded from recall and reported
as expired by diagnostics.

## Backend and synchronization

The runtime calls a small `TeamMemoryBackend` interface. SQLite is the default
local implementation and is configured through the user data directory (or an
explicit user-level database path). It uses WAL, a five-second busy timeout,
and bounded write retries, so task-end capture, publish, and feedback can
share one host without treating `database is locked` as a normal outcome.

Use explicit portable bundle operations when agents run in different
containers or machines:

```text
team-export --output <bundle.json>
team-import --input <bundle.json>
```

Import is an idempotent merge keyed by stable memory id and causal revision.
Bundle schema 3 includes an append-only `memory_revisions` log. An incoming
revision is applied when the local revision is in its retained ancestor chain,
so `v1 -> v3` is a valid fast-forward even if `v2` was missed. Concurrent
branches and stale revisions are reported, not selected by wall-clock
last-write-wins. Feedback carries a stable `feedback_id` and `origin_node` for
cross-machine deduplication. This is file-based synchronization, not a hosted
database service.

Activation records `reviewed_by` and `activated_at` while preserving the
original `author_agent`.

## Context hydration

Team Memory is recalled automatically: `memory_search` defaults to
`scope="auto"` and returns a `team` group alongside `repository` and `memory`.
That group holds `active` and `candidates` separately, each bounded by `limit`.
Team records are experience provenance, not Git citations, so they are
deliberately absent from `verified` — never present one as a source citation.

The lower-level `memory_context` remains available in the CLI (`context`) for
callers that want the Team Memory sections split by type:

- `repository_evidence`: source-backed, commit/path/line citations;
- `decisions`, `failures`, `solutions`, `discoveries`, `handoffs`:
  experience-backed records with their own provenance;
- `repository_candidates` and `team_candidates`: leads that must not be stated
  as facts without checking;
- `diagnostics`: lexical/semantic capability, counts, parallel recall, and the
  explicit no-score-fusion policy. The current context mode is
  `multi-source-lexical`; it is not an embedding-backed hybrid retriever.

The package is a retrieval fusion seam, not a black-box RRF score. A Git
citation remains a Git citation; an experience record remains an experience
record. Agents should explain which section supports each claim.

## Automatic capture boundary

Host lifecycle capture may write raw L0 and an L2 candidate, but it should not
turn every assistant message into Team Memory. A task-end extractor should
publish only reusable decisions, discoveries, failures, solutions, or handoffs
and use an idempotency key. L3/profile promotion remains a separate explicit
operation.
