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

## Four standalone memory layers

The default in-process runtime owns four distinct SQLite-backed surfaces. The
layer names are compatible with the MemoryCore vocabulary, but the standalone
implementation does not require a vendor service:

| Layer | Meaning | Current adapter behavior |
| --- | --- | --- |
| L0 | Raw conversation/message memory | Durable SQLite write and read-back verification. |
| L1 | Atomic memory extracted from conversation | Deterministic atomic projection with write and read-back verification. |
| L2 | Scenario/generated long-lived context | Automatically projected as an unaccepted candidate; explicit review changes it to `accepted`. |
| L3 | Profile/core memory | Written only by explicit promotion and verified by a second read. |

The standalone ingest transition is therefore:

```text
explicit session input
  -> durable L0 write
  -> verify L0 ids
  -> durable L1 write and read-back
  -> project an unaccepted L2 candidate
  -> explicit review/promotion writes L3 and verifies it
```

An optional native compatibility backend may expose asynchronous or unsupported
states, but that does not change the standalone contract. The default local
runtime reports the actual record counts and statuses; endpoint readiness alone
never counts as populated memory.

## Agent rules

- `memory_init` registers a user-supplied repository source; it is not a
  memory-ingest operation.
- `memory_ingest` is a write and requires an explicit user request. It accepts
  a session/conversation payload, not an inferred fact from ordinary chat.
- After ingest, report the actual `l0_verified`, `l1_verified`, and L2
  candidate receipt. Do not call an L2 candidate an accepted fact.
- Report L2/L3 only when `doctor` or a subsequent `get` response returns the
  record and its status. `supported_layers` and `reachable=true` are readiness
  facts, not data-quality or population facts.
- A raw L0 result means “the conversation contained this”; without linked
  repository evidence it must not be presented as a Git-backed project fact.
- Delete synthetic test conversations after an explicit isolated test when the
  backend supports deletion. Never write test data into the canonical Git repo.
