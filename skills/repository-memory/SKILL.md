---
name: repository-memory
description: Answer questions about this project, its history, past conversations, and prior team decisions from source-backed evidence with verified citations. Use whenever the user asks what something is, why it was decided, what happened before, or what the team already knows.
---

# Repository Memory

## The one call

```text
memory_search(query=<the user's question, verbatim>)
```

That is the whole interface for reading. It searches three planes at once and
keeps them apart:

- **`verified` / `results` / `answerable`** — repository evidence with
  commit/path/line citations. This is the answer surface.
- **`groups.memory`** — durable conversation memory (L0–L3).
- **`groups.team`** — reviewed team decisions, failures, solutions,
  discoveries, and handoffs, split into `active` and `candidates`.

Send the question as the user wrote it. Do not translate it into a filename,
do not pre-select a scope, and do not call `memory_doctor` first. Ask, then
read what came back.

Use the host's namespaced name when there is one (`repository-memory__memory_search`).
A bare `memory_search` may belong to a different backend; do not substitute it.

## Capability boundary

It answers from the registered stores and nothing else: the Git corpus
(cited), reviewed team records, and captured conversation memory. `abstain`
means those stores hold no evidence for the claim — that is the tool working,
not failing. It never guesses, and a retry padded with invented specifics
will not change the outcome.

Consulting it is a judgment call, not a ritual. Reach for it when the
question is about this project or team — what happened, who did what and
when, why something was decided, what the team already knows. Skip it for
general knowledge, the outside world, or content already open in the
session; answering those directly is correct use of this Skill.

Every host reaches the same runtime and result contract: Claude Code and
Codex through this Skill plus the audited `repository-memory` MCP server,
OpenClaw through the native `repository-memory__memory_*` tools, and any
other harness through the stdio MCP (`repository-memory mcp`) or the bundled
CLI (`scripts/repository-memory.py search|get|doctor --json`).

## Reading the answer

1. Answer from `answerable`/`results`, not from the mere presence of `verified`.
2. `abstain` covers the repository plane only. Before giving up, read
   `answered_by`: if it names `memory` or `team`, answer from that group and
   label it as conversation memory or a team decision, not as a citation.
3. Call `memory_get` when the claim is important, compound, or time-sensitive.
4. Report source, commit, path or memory layer, line range, and freshness.
5. If `claim_support` is `partial`, state only the supported subclaim.
6. If `answered_by` is empty, say the evidence is insufficient. Do not
   substitute a similar document.
7. If `citation.pinned` is false (`evidence_status: worktree`), the excerpt was
   read back from the working tree and matched, but the source has uncommitted
   changes. Answer from it, and attribute it to the working tree rather than to
   a commit.
8. An abstention on a symbol-heavy or paraphrased phrasing is evidence, not a
   dead end: the candidates' `support.unmatched_terms` name exactly which
   words the corpus lacks. Re-ask once in the document's own vocabulary — one
   plain sentence — and never pad the retry with invented specifics.

`verified` means a citation or memory identifier checked out — not that one
excerpt proves every part of a compound question. `candidates` are leads:
stale, generated, inferred, or citation-incomplete content is not a fact until
`memory_get` validates it.

Team records are experience provenance, never Git citations. Never present one
as a source citation.

Report the actual `retrieval_mode`. If it says `lexical` or `keyword-only`,
do not call the retrieval semantic or hybrid.

If the response reports the source as `not_configured` or stale, then call
`memory_doctor`, and `memory_sync` if it asks for one. If neither the MCP nor
the bundled CLI exists, report `not_installed` — a config file or a
model-written receipt is not proof of installation.

## Memory layers and writes

```text
L0  raw conversation/message      explicit ingest or opt-in capture
L1  extracted atomic memory       read-back verified before use
L2  scenario/context              candidate until explicit review
L3  profile/core memory           explicit accept/promote plus read-back
```

A reachable endpoint does not prove a layer holds anything. Use doctor or get
to separate capability, readiness, population, and read-back.

Search and sync are read operations. Only run `ingest-session`, `feedback`,
`promote`, Team Memory publish/activate, or host capture when the user or an
explicit task-end workflow asks for a write. New memories stay
candidate/pending until the documented review step; ordinary conversation
never writes L3. Never rewrite the canonical repository.

## Tool boundary

`read`, `grep`, `git`, `exec`, tests, and debugging remain valid for coding
work. For a project-fact answer, do not use them as an undocumented substitute
for this MCP, and never describe a directly-read file as an MCP citation.

## Advanced

Only when the single call is not enough:

- `memory_get` — resolve one result and its evidence window.
- `memory_doctor` / `memory_sync` — diagnose or refresh sources.
- `memory_timeline` / `memory_observe` — how a conversation memory was formed;
  provenance for the memory lane, not a Git citation.
- `memory_reflect` — bounded derived summary, always candidate-labelled.
- `scope="repository" | "memory" | "all"` — pin a single plane. The default
  `auto` is correct for ordinary questions.
- `local=true` — only when the user explicitly asks for the offline worktree.
  A dirty worktree is not an acceptable default source for project facts.

Read the references only when needed:

- `references/result-contract.md` — `verified`, `answerable`, candidates;
- `references/citation.md` — citation validation;
- `references/retrieval.md` — routing and retrieval-mode honesty;
- `references/entity-and-memory-model.md` — L0–L3 semantics;
- `references/write-policy.md` — explicit writes and promotion;
- `references/automatic-capture.md` — optional host lifecycle capture;
- `references/team-memory.md` — shared decisions and handoffs;
- `references/operations.md` — CLI commands and MCP handshake;
- `references/host-guard.md` — host routing and the advisory guard;
- `docs/team-memory-git-sync.md` — Git-backed multi-agent sync and review.
