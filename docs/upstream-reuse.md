# Upstream memory implementations we reviewed

The repositories below are reference snapshots only. They are kept outside
this repository under the user's local knowledge workspace so they cannot
silently become a second runtime or a second canonical source.

| Project | Reference commit | License | What we reuse | What stays out of the default install |
| --- | --- | --- | --- | --- |
| [MemOS](https://github.com/MemTensor/MemOS) | `b41c8996a8dcb9df81998cced68d11457ce950c3` | Apache-2.0; local plugin MIT | L1/L2/L3 vocabulary, ordered timeline, tool outcome feedback, lazy local model loading | Neo4j/Qdrant deployment, its competing OpenClaw plugin, relaxed induction thresholds |
| [Cognee](https://github.com/topoteretes/cognee) | `b948f88d48befe58e8b10e6b833adacdce4e0ddd` | Apache-2.0 | Explicit graph/relationship expansion as a future provider seam | Mandatory graph/vector service and a second document store |
| [TencentDB Agent Memory](https://github.com/Tencent/TencentDB-Agent-Memory) | `97f94654280b2932c35ba4806a491999ed244cc9` | MIT | Layer lifecycle and explicit read-back discipline | TencentDB service, Wiki/TCVDB and its memory slot |
| [Mem0](https://github.com/mem0ai/mem0) | `001c235229be8795e3834520467bd0d661ed8f34` | Apache-2.0 | Small provider boundary and feedback-oriented API shape | Hosted/vector database defaults and a competing memory authority |
| [MemPalace](https://github.com/MemPalace/mempalace) | `639c69a1d6be41a04964ceb72a3d29d6f45629e9` | MIT | Local-first retention and original-record preservation ideas | Its server deployment and unrelated personal-memory semantics |
| [Hindsight](https://github.com/vectorize-io/hindsight) | `ec9cc702ec55898bcac0db9c9e598305772ad7ad` | MIT | Retention/recall concepts, and the pattern of treating CJK segmentation as a named, swappable tokenizer choice | No code copied into the runtime |
| [jieba](https://github.com/fxsjy/jieba) | `0.42.1` (PyPI) | MIT | CJK query segmentation, as an optional `cjk` extra | The `lac_small` paddle models and the paddle backend, which are never imported; no user dictionary is loaded |

## Decision

We are not installing six plugins. `repository-memory` remains the only public
OpenClaw/MCP entrypoint. The Git repository and its commit/path/line citation
remain authoritative for project facts. Conversation memory remains a
separate local lane.

The first concrete reuse is in the standalone runtime:

- results expose `tier`, `ref_kind`, `ref_id`, and bounded `snippet` fields;
- `memory_timeline` exposes an ordered L0/L1 trace for diagnosis and replay;
- `memory_observe` and `memory_reflect` expose the retain/recall/reflect/observe
  split as explicit operations; reflection is always generated/candidate and
  never accepted automatically;
- standalone local recall uses deterministic recency decay plus MMR-style
  diversity selection, with ranking features returned for audit;
- L2 is labelled `policy`, and L3 is labelled `world_model`, while the
  canonical L0/L1/L2/L3 names remain unchanged;
- all of this is local SQLite and does not require a daemon or model download.

The defaults from upstream were not copied blindly. In particular, a low
induction threshold can create impressive-looking but noisy L2/L3 records. Our
runtime keeps explicit candidate/accepted states and read-back verification.

## What was added after the comparison

The useful parts are implemented in the independent local core rather than
left as adapter promises:

- MemOS-style lifecycle metadata is represented by layer, status, source,
  session, version and read-back fields; feedback remains explicit and does not
  silently rewrite a fact.
- Hindsight's separation between retention, recall and reflection is reflected
  in separate `memory-retain`, `search`, `memory-observe`, `memory-reflect`,
  and supervisor paths.
  Reflection/supervision remains optional and cannot auto-accept L2/L3.
- Cognee's explainable relationship idea is implemented as a tiny derived graph:
  Markdown links and explicit local file references are indexed, relationship
  queries expand one hop, and results expose `related` citations. There is no
  graph server and no inferred edge pretending to be evidence.
- The same graph seam now exists for native local memory: only explicit
  `source_record_ids`/`related_ids` metadata creates `memory_links`, and
  `get/search` returns the one-hop relation for inspection.
- Mem0-style identity/session/run scoping and deduplication are retained in the
  standalone memory and team-memory stores.
- TencentDB's layer readiness versus population/read-back distinction is kept
  as a hard contract: an available endpoint is not counted as useful memory.
- TencentDB's capture boundary is now implemented in the public OpenClaw
  extension: when the host exposes a pre-turn message count or timestamp
  cursor, capture keeps only the new turn, restores the original user text,
  and removes injected recall from the durable input. Hosts that do not expose
  those fields retain the bounded safe fallback.
- MemOS' short-CJK retrieval lesson is implemented without importing its
  runtime: the derived trigram index includes the relative path as searchable
  text, so a person/card name that appears only in a filename can still reach
  the citation-first scorer. This is a recall fix, not a semantic claim.
- MemOS' hybrid lane is also used as an explicit rescue path rather than an
  always-on startup cost: large sources begin with lexical/path retrieval and
  load the optional deterministic projection only after a lexical miss. This
  preserves the useful fallback without making a fresh snapshot appear hung.
- Segmenting a CJK query before lexical scoring is now reuse rather than
  reference. Every upstream implementation that works in Chinese does it:
  TencentDB Agent Memory tokenizes with `@node-rs/jieba` before BM25, MemOS
  calls `jieba.lcut` in its `FastTokenizer`, and Hindsight exposes `jieba` and
  `chinese_compatible` as index-time tokenizer choices. MemPalace's `\w{2,}`
  is the counter-example, and it is English-only. `tokenize_query.py` follows
  the same shape with two constraints of our own: segmentation runs on CJK
  runs only, because jieba shreds identifiers such as `rlvr-auto-survey`, and
  adjacent segments are rejoined, because a compound absent from the
  dictionary comes back split and rejoining recovers it without a user
  dictionary or any list of project names. jieba stays optional: this package
  declares no hard dependencies, and retrieval falls back to character n-gram
  carving when the extra is not installed.
- Date normalization is ours, not upstream, and it is held to the same rule as
  everything else in retrieval: it is a grammar over digits and 年/月/日, so it
  does not grow when a project, a person, or a system is added. People ask
  `8月18日` and Markdown headings say `## 2026-08-18`; without normalizing, the
  term was required, occurred in no document, and the question abstained against
  a file that answered it. The year is never inferred from the clock, because
  guessing it answers a different question whenever the corpus spans more than
  one year.
- The `gateway` embedding provider reuses one thing and only one thing: the
  OpenAI `/embeddings` request and response shape, which every gateway we can
  reach already speaks. It adds no dependency — `urllib` and `json` are enough,
  so `dependencies` stays empty — and no resident memory, which is why it
  exists: the local neural option needs 1.5–2 GB to hold its weights and the
  machine this runs on does not have it. The credential is never written to
  configuration; what gets persisted is the *name* of the environment variable
  to read, because a config file is copied, diffed and backed up, and a secret
  written into it leaks by every one of those routes.

The repository index stores conservative date anchors from paths and headings,
plus explicit local references. Temporal routing uses those anchors instead of
dates buried in arbitrary evidence text. This improves latest/report queries
without changing canonical Git files.

## Deliberate non-goals

We did not copy Neo4j/Qdrant/Postgres deployment, a second canonical document
store, opaque graph edges, automatic LLM memory acceptance, or a mandatory
remote embedding service. The default remains zero-service and citation-first.
The current `builtin-char-ngram-v1` projection is a deterministic local recall
lane; it is not a neural embedding model and is reported as such.

"Mandatory" is the operative word in that list: a remote endpoint may now be
configured, and on this machine one is. It stays a non-goal in the sense that
matters — nothing is required to reach the network, an unreachable endpoint
degrades to the local projection instead of failing the query, and no embedding
provider of any kind can decide whether a question is answerable. Embeddings
widen the candidate set; claim support and citations decide the answer. Verified
live rather than asserted: a query of invented tokens admitted five candidates
through the gateway and still produced zero answerable results and abstained.

## Reference locations

The clone locations are user-level reference material, not import paths used
by the runtime. `<references-root>` stands for wherever you keep reference
checkouts; the resolved path is deliberately absent, because a maintainer's
home-directory layout is user data and this file is public:

```text
<references-root>/MemOS-reference
<references-root>/memory-references/cognee
<references-root>/memory-references/tencentdb-agent-memory
<references-root>/memory-references/mem0
<references-root>/memory-references/mempalace
<references-root>/memory-references/hindsight
```

Do not add resolved absolute paths to Skill instructions or user
configuration — or to this file. `personal-absolute-path` in
`tools/scan-tree.sh` now fails CI if they come back.
