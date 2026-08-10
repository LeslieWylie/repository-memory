# Entity and memory model

This reference describes the current implementation. It is intentionally
explicit about what the runtime does not do, so an agent does not turn a
lexical match or a layer readiness check into an unsupported claim.

## Repository entities

The repository path is the canonical fact source. The current local index
stores a source revision and document records containing `path`, `text`, and
`size`. It does not persist a separate NER table, alias dictionary, embedding
vector, or relation graph.

At query time the fallback performs conservative structural routing:

1. Preserve the user's original query.
2. Keep normal ASCII/path/date tokens and inflectional forms.
3. Split a contiguous CJK run into short 2/3/4-character fragments, stopping
   at common temporal/question boundaries. This is tokenization, not entity
   recognition.
4. Score source path and filename, document text, early Markdown headings,
   generic directory layers, date paths, and corpus-local rarity.
5. Prefer temporal layers such as `standup`, `daily`, `diary`, `journal`,
   `report`, or `weekly` only when those directories exist in the source.

The result may expose a path/file-name or layer match that is useful for
retrieval. It does not establish that the matched string is a person, paper,
model, team, or project, and it does not establish a relation between two
records. For relationship questions, verify each side and the relation in
separate evidence windows. Never invent aliases or graph edges from a short
token overlap.

## Four MemoryCore layers

When a native memory adapter is configured, the layer names describe distinct
surfaces:

| Layer | Meaning | Current adapter behavior |
| --- | --- | --- |
| L0 | Raw conversation/message memory | Durable write through the conversation API; searchable with its native locator. |
| L1 | Atomic memory extracted from conversation | Searchable through the atomic API; after a write, extraction is normally asynchronous and must be observed as `pending`, `verified`, or `unknown`. |
| L2 | Scenario/generated long-lived context | Read through the scenario API. `generated`, `accepted`, and `pending` are status signals, not proof of truth. The current adapter does not synchronously create an L2 record during ingest. |
| L3 | Profile/core memory | Read through the core/profile API. A ready endpoint does not mean a new session was promoted into the profile. |

The native ingest transition is therefore:

```text
explicit session input
  -> durable L0 write
  -> verify L0 ids
  -> report L1 pending/unknown
  -> later search/doctor/read observes L1, L2, or L3 if the backend actually produced them
```

The local fallback, used when no native backend is reachable, is deliberately
smaller: it can persist and search local L0/L1 records, while L2/L3 are
reported unsupported. Do not call this fallback a four-layer memory system.

## Agent rules

- `memory_init` registers a user-supplied repository source; it is not a
  memory-ingest operation.
- `memory_ingest` is a write and requires an explicit user request. It accepts
  a session/conversation payload, not an inferred fact from ordinary chat.
- After ingest, report the actual `l0_verified` and `l1_status`. Do not claim
  L1 is complete because the write request returned successfully.
- Report L2/L3 only when a subsequent API response returns the record and its
  status. `supported_layers` and `reachable=true` are readiness facts, not
  data-quality or population facts.
- A raw L0 result means “the conversation contained this”; without linked
  repository evidence it must not be presented as a Git-backed project fact.
- Delete synthetic test conversations after an explicit isolated test when the
  backend supports deletion. Never write test data into the canonical Git repo.
