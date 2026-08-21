# Changelog

## 0.7.16

- Give every retrieval plane one query tokenizer (`tokenize_query.py`) with
  optional jieba word segmentation behind a new `cjk` extra. Previously four
  call sites tokenized CJK three different ways, and two of them — Team Memory
  and the compatibility L0/L1 store — did not segment at all, so a Chinese
  question arrived as a single clause-length token that matched nothing. Those
  two planes were unreachable in Chinese, including under `scope=auto`.
- Skip the SQLite FTS5 pre-filter in Team Memory and local memory search when a
  query term contains CJK. The stock `unicode61` tokenizer indexes a whole CJK
  run as one token, so a MATCH for a segmented term returns nothing; both call
  sites already re-match with substrings, which is CJK-safe.
- Stop manufacturing terms that span the seam between a question's scaffolding
  and its subject (`是怎么配置`, `做了`). Such a term occurs in no document, so
  requiring it held claim coverage below 1.0 and produced abstention by
  tokenizer rather than by evidence.
- Report the active tokenizer in search diagnostics and doctor, beside
  `semantic_available`: whether jieba is installed changes what a Chinese query
  retrieves, so a retrieval measurement that omits it is not attributable.
- Count the citation path as claim-support evidence for core-normalized results
  as well as adapter results, which already had it. Retrieval indexes
  `"<path> <text>"`, so a document could be found *by* its path and then be
  unable to prove the term that found it.
- Normalize Chinese date expressions to the form the corpus writes: `8月18日`
  becomes `08-18` and `2026年8月18日` becomes `2026-08-18`, and evidence that
  writes a date in Chinese proves a query that normalized to ISO. People ask one
  way and Markdown headings are written the other, so the term was required,
  occurred in no document, and abstained. This is a grammar over digits and
  三 characters, not a vocabulary: it does not grow when a project or a person is
  added. An unqualified `8月18日` is not given a year, because guessing one
  answers a different question whenever the corpus spans more than one year.
- Stop splitting a hyphenated term into purely numeric parts. `08-18` became
  `08` and `18`, which matched every merge-request number, GPU size and line
  count in the source and buried the single line carrying the date — the excerpt
  picker then cited a section eleven months away from the question. The split
  exists for `long-context`, which decomposes into concepts; digits do not.
- Let a date filter read the heading anchors the index already derives, not only
  the path. `local_index._document_dates` collects dates from paths *and*
  headings, but the filter read the path half alone, so a document dating its
  sections as `## 2026-08-18` was excluded outright. That made the year-qualified
  question strictly worse than the bare one.
- jieba remains optional and is never downloaded; `dependencies` stays empty
  and the character n-gram path is exercised by the test suite on its own.
- Add a `gateway` embedding provider: any OpenAI-compatible `/embeddings`
  endpoint, configured with `semantic configure --provider gateway --endpoint
  ... --dimensions ...`. The local neural option needs 1.5–2 GB resident to load
  its weights, which this machine does not have; a remote endpoint costs no
  resident memory at all. The credential is read from an environment variable
  whose *name* is what gets persisted — a config file is copied, diffed and
  backed up, and a secret written into it leaks by every one of those routes.
  Readiness is probed once and cached on disk with exponential backoff, because
  the CLI is spawned per request and must not pay a network round trip to
  discover an endpoint is still down.
- Encode a corpus into a packed `array('f')` rather than a list of float lists.
  On the 37k-document source the nested form costs roughly 600 MB against 75 MB
  packed, and the write path materialized a second copy of it again to serialize.
- Record the embedding spec the vectors were *actually* produced with, not the
  one that was requested. An optional provider can fail after the readiness
  check and leave the corpus to the local projection; storing the requested
  triple then claims a cache that does not exist and rebuilds the whole index on
  every later search.
- Load an existing semantic cache for a large source instead of deferring it.
  The deferral exists so a first query does not pay for encoding the corpus, but
  it also skipped the cache that encoding had already produced, so an index
  built once was never used: measured on the live 1697-document source, the same
  query took 309.7 s and then 3.5 s. A cache whose provider/model/dimension no
  longer match the configured ones is still deferred rather than scored, because
  the query is encoded in the current embedding space and comparing two spaces
  produces confident nonsense.
- Treat a word the segmenter returned as a claim the user made, and only the
  joins built around it as guesses. Every CJK term was previously marked
  "carved", so the unreachable probe dropped any of them the corpus lacked —
  including the only specific word in the question. Measured live: `腌制泡菜的
  传统做法` had `腌制`, `泡菜` and `腌制泡菜` all dropped at df 0 and was answered
  `direct` with coverage 1.0 out of five RLVR notes, on the strength of the one
  generic phrase left standing. When an unreachable join is dropped, the
  segmented words it absorbed are restored to the requirement. The builtin
  n-gram path, where every fragment genuinely is a guess, is unchanged.
- Let the endpoint credential be read from a file this configuration only points
  at (`--api-key-file`, with `--api-key-json-path` when that file is JSON owned
  by another tool). Naming an environment variable is the better bargain and
  still takes precedence, but an agent host launched from a GUI inherits no
  shell environment, so a credential-by-name scheme resolves to nothing there
  and the remote provider silently stops being used at exactly the moment it was
  configured. Only the path is persisted; the secret stays in the file that
  already held it. Every unreadable shape — missing file, not JSON, wrong key
  path, non-string leaf — yields no credential rather than an exception, because
  the local projection still answers the query.
- Raise the public gate's Chinese coverage from one query to five. A tokenizer
  change cannot be verified by a set that contained a single Chinese positive.
  The added queries vary the interrogative shape rather than the subject,
  because the shape is what the closed stop set has to absorb, and one of them
  carries dotted version numbers so that rule is guarded beside CJK scaffolding.
  The queries themselves are not quoted here: three of them take this file as
  gold, and a gate whose evidence repeats its own queries measures nothing.
  Measured on both tokenizer paths — every gate metric stays 1.0 with jieba and
  without it.
- Stop discarding evidence that was proved, because the tree it came from was
  dirty. A citation whose excerpt has just been read off disk and matched was
  being marked invalid *and* stale whenever any file anywhere in the source had
  uncommitted changes, which zeroed `verified`, `results` and `answerable`.
  Measured on this repository: a question whose answer sat in three documents at
  `claim_support=direct, coverage=1.0` returned nothing, because an unrelated
  file was uncommitted. The two facts are now separate — claim support still
  decides answerability, while the missing commit pin is reported as evidence
  quality (`citation.pinned=false`, `evidence_status="worktree"`). It is
  deliberately not called stale: stale evidence may no longer say what it said,
  whereas this evidence is exactly what is on disk. `get` reaches the same
  verdict independently, so a hit that answers does not fail when the caller
  asks to see it. Nothing about the excerpt check, secret exclusion or negative
  abstention changes, and the gate — which evaluates a clean revision — is
  unmoved on both tokenizer paths.
- State the capability boundary in the Skill instructions: which stores answers
  come from, that abstention is the tool working rather than failing, and that
  consulting memory at all is the host model's judgment call. The same section
  names the one runtime every harness reaches — this Skill plus the audited MCP
  server for Claude Code and Codex, the native tools for OpenClaw, the stdio
  MCP or the bundled CLI for anything else — because a host deciding *whether*
  to call the tool never read the references where those facts lived.
- Let the supervisor's provenance gate accept a memory lineage, not only a Git
  citation. Team records are experience provenance by contract, and the
  auto-capture path writes `source_memory_id`/`observed_at`/`run_id` rather
  than citations — so the citations-only hard check held every captured
  candidate forever regardless of the model's verdict; measured on the live
  store, 284 of 289 records were unactivatable by construction. The gate now
  checks what it was always for — that the record can say where it came from —
  and reports `provenance_kind` (`citation`, `memory-lineage`, `none`) in the
  receipt. A record with neither still holds even when the model says accept,
  and nothing else about review changes: secret and content checks, mandatory
  model review, the confidence floor, and explicit `--apply` all remain.
- Force UTF-8 on the CLI's stdout/stderr. On Windows a piped stdout defaults to
  a legacy code page, so printing a JSON answer that carries CJK content raised
  UnicodeEncodeError — which is a ValueError, so the generic error handler
  swallowed it into a silent exit 2. Windows CI reported the team gate as
  failed with an empty stderr while the identical evaluation passed in-process;
  with Chinese now in the team evaluation set, every such command on Windows
  was affected.
- Close the descriptor `mkstemp` returns before renaming the exported Team
  Memory file over its destination. The leaked open handle made `os.replace`
  fail on Windows with WinError 32, so `team-export` there died on its first
  record — on every platform it also leaked one descriptor per exported file.
- Give every text-mode subprocess call an explicit UTF-8 encoding. The child
  processes already answer in UTF-8, but a text-mode pipe decodes with the
  locale code page, and on Windows the pipe is drained by a reader thread — a
  decode error kills that thread silently and `communicate()` returns `None`
  for the stream, so a successful `git`/CLI call with CJK output looked like a
  process that produced nothing. The gate test failed exactly this way on
  Windows CI while the child exited 0.
- Activate every projection of a team memory together. One memory can sit in
  the store twice — the local original and a central wrapper hydrated from the
  canonical repository, linked by `provenance.source_memory_id` — and review
  activated only the row it was pointed at. The exporter prefers the original
  for its richer provenance, so the canonical file kept saying `candidate` and
  the activation never reached another agent: measured live, 71 activations
  moved 3 files. `activate` now transitions the linked projections in the same
  write and reports them as `activated_siblings`.

- Tell the reader what to do with an abstention. A fresh-host tester asked a
  symbol-heavy question, got a correct abstention, and read it as phrasing
  sensitivity — while the response had already named the missing words in
  `support.unmatched_terms`. The Skill instructions now say to re-ask once in
  the document's own vocabulary, guided by those terms, and still never to pad
  a retry with invented specifics.

- Let a hydrating store accept the `scenario` records this pipeline itself
  exports. The supervisor writes accepted L2 scenarios and the exporter
  publishes them under `l2/accepted`, but the store's type vocabulary predated
  L2 export — so every host's pull reported `failed: 1` on the first accepted
  scenario, and the JSON did not say which file. Hydration failures now carry
  `failures: [{path, error}]`, and the whole per-file body sits inside the
  guard: a malformed `confidence:` used to escape the narrower try and abort
  the entire pull.
- Report `team_memory_distinct` in team-status: memories grouped by canonical
  identity, beside the raw row counts. One memory can sit in the store as a
  local original plus a hydrated central wrapper, so row counts overstate the
  plane — measured: a fresh host hydrated 75 active canonical files and was
  told to expect "140+" because another machine's row count said 143.

- Add `team-publish`: rebase-pull the team repository, run team-sync, commit
  only what it wrote under `knowledge/`, and push — as one explicit command
  with a JSON receipt. The capture hook deliberately never commits or pushes,
  and every node closed that gap with a hand-written shell script passed
  around in chat; the second host literally received its copy by prompt.
  Publishing stages nothing outside `knowledge/`, so a stray file in the
  clone can never ride along, and review stays out of it: activation remains
  an explicit supervised step.
- Preflight the git identity in `team-publish` and answer with
  `missing_git_identity` plus the exact commands to run. The first fresh-host
  publish died on git's localized "Author identity unknown" buried in a
  generic error. Documented beside it: inbox directories group by each
  record's capturing-agent id (carried in the record, so every node computes
  the same path), not by the node's configured agent id — name agents
  globally, or two nodes' `main` agents will share one inbox.
- Conform to the surfaces the ecosystem actually reads, measured against
  TencentDB Agent Memory and MemOS: MCP tools now carry `title` and
  `annotations` (all read-only except `memory_sync`, the one open-world
  tool), the CLI answers `--version` with the release and MCP revision, and
  the Skill frontmatter carries `license` plus `metadata.version` per the
  open Agent Skills spec — with a delivery test pinning frontmatter, VERSION
  and pyproject to one number. Neither reference ships MCP (TencentDB is
  proxy-based by design, MemOS documents none) or a first-party CLI, so those
  axes stay ours; what they do better is root-level install docs and
  bilingual entry points, adopted as `INSTALL.md` and `README_CN.md`.
- Force-materialize remote snapshots and verify them before claiming the
  commit. A checkout killed mid-write — a caller timeout is enough — leaves
  the snapshot's index and HEAD advanced while files on disk stay old, and
  every later plain checkout sees nothing to do, so the staleness is
  permanent and invisible. Measured live: a snapshot labelled one commit
  served standup files from a week earlier, and every question about that
  week abstained against a corpus that had the answer. The checkout now runs
  `--force` (a snapshot worktree holds no legitimate local edits) and the
  view falls back to the local worktree, with a reason, when `status
  --porcelain` is not empty afterwards.
- Weight a Markdown heading that carries a query term above a body line that
  mentions it in passing, and cite the section the heading opens. Four lines
  tied at one occurrence each and the tie-break handed the citation to the
  earliest — an incident retro inside the 08-20 section that referenced the
  date once — while `## 2026-08-18`, the section actually about the queried
  day, sat at line 77. The question was answered partial out of the wrong
  section; with the heading weighted it answers direct out of the right one.
  A heading names what its section is about; that is the same structural fact
  the index already trusts when it derives date anchors from headings.
- Let one install command configure the whole team-grade setup: `--cjk`
  (best-effort jieba for the runtime interpreter, PEP 668 reported not
  raised), `--semantic-*` (encoder provider/model/endpoint/dimensions and a
  credential *name or path*, configured before source registration so the
  first sync builds vectors with the right provider), and
  `--team-repository`/`--team-agent-id` (clone under the data root — publish
  commits from it, caches may be wiped — then team-configure and a first
  hydrate; the receipt carries a ready `publish_cron` line). Previously this
  was a hand-relayed checklist per host: the second host received it as five
  chat steps and every step grew its own trap.
- Gate the team plane's answerability on claim support, not on having
  matched. The team backend's lexical match returns any active record sharing
  a term with the question, and `answered_by` counted that as an answer:
  measured live with human phrasings, "我们公司什么时候上市" came back
  `answered_by=['team']` carrying a sync-timeout ops note that shared one
  generic word — a host following the Skill would have fabricated an IPO
  answer from it. Every active team record now carries `support` scored by
  the same claim rule the repository plane answers under, the group sorts by
  coverage, and only direct support makes the plane answerable; weaker
  matches stay visible as leads.
- Add the colloquial register to the closed interrogative/deictic classes
  (`干嘛`, `干啥`, `咋样`, `怎么样`, `得怎么样`, `啥时候`, `明天/前天/后天`) and accept
  `号` as the spoken form of `日` in the date grammar. Measured on live human
  phrasing: `武垚乐昨天在干嘛` and `GLM 迁移做得怎么样了` both abstained with
  the colloquial fragment as the unmatched claim, and `8月20号` failed where
  `8月20日` worked. Same closed-class boundary as before: these are function
  words and a date grammar, not a vocabulary that grows with content.

## 0.7.15

- Protect existing canonical Team Memory Markdown during automatic hydration
  and sync; local projections can no longer overwrite reviewed provenance,
  citations, or lifecycle metadata.
- Align the OpenClaw extension and AML submission documentation with the
  packaged runtime version.

## 0.7.14

- Add an explicit `default_source` routing boundary for multi-repository
  configurations. Unqualified CLI, MCP, and plugin queries now use the
  configured source instead of silently mixing every registered repository;
  explicit `--source` and `--root` continue to support multi-repository work.
- Surface the selected default source in `source list` and doctor config
  diagnostics.

## 0.7.13

- Add explicit read-only `observe` and candidate-labelled `reflect` operations
  to the standalone runtime, CLI, MCP, and native OpenClaw tools.
- Add deterministic local-memory recency decay and MMR diversity selection;
  relevance remains dominant and the ranking features are returned for audit.
- Add an explicit-memory `memory_links` table for one-hop provenance and
  relationship citations. It is derived from IDs already present in metadata,
  never inferred from vector similarity or a graph service.
- Extend the installer and selected-agent allowlists for the two native tools.

## 0.7.12

- Increase the OpenClaw native plugin's default runtime timeout to 60 seconds.
  Cold `doctor` calls on multi-source repositories can take longer than the
  previous 15-second default while checking remote snapshots; this avoids a
  false unavailable result without changing the shared CLI runtime.

- Keep native tools and the stdio MCP on the same runtime and retain the
  advisory, non-blocking lifecycle hooks.

## 0.7.7

- Port the useful MemOS Local lifecycle mechanics into the independent
  provider-free runtime: episode/turn identifiers, conservative turn relation
  classification, feedback-weighted trace values, and time-decayed priority.
- Add an evidence-backed L2 policy candidate pool requiring multiple distinct
  episodes and retaining source record IDs.
- Keep Git citation retrieval, the CLI/MCP contract, and the canonical source
  independent from the MemOS Node package.

## 0.7.5

- Preserve explicit OpenClaw turn boundaries during automatic capture: use the
  host's position/timestamp cursor and original user text when available, so
  recalled context and old session messages do not become new memory.
- Include relative paths in the disposable large-repository FTS stream. Short
  CJK/person-name queries can now reach filename-anchored evidence instead of
  being discarded before deterministic ranking.

## 0.7.4

- Add conservative date anchors and explicit local-reference metadata to the
  disposable repository index.
- Improve latest/report routing from headings and explicit date fields without
  treating arbitrary body dates as document dates.
- Add explainable one-hop relationship expansion and `related` citations for
  explicit local links, without a graph service or opaque score fusion.
- Preserve the zero-service, citation-first default and report the builtin
  projection as non-neural.

## 0.7.3

- Align the AML wrapper with the current public contract: accept any non-empty
  message role and normalize Unix-millisecond source timestamps.
- Use source event time in `created_at` and add a bounded recency signal for
  explicit latest/recent queries.
- Keep the public Add response to the exact declared fields while retaining
  internal write/read-back verification.
- Add a submission-ready code-route runbook without claiming local fixture
  scores as leaderboard results.

## 0.7.1

- make `repository-memory benchmark --suite public --json` discover the
  checked-out public repository root automatically;
- keep explicit `--root` and manifest-root behavior unchanged.

## 0.7.0

- add a dependency-free synchronous Agent Memory Leaderboard Add/Search
  wrapper with user isolation, auth, health and Docker submission instructions;
- expose `repository-memory-aml` as a packaged entry point;
- keep AML ingestion on the standalone L0/L1 path without changing the
  citation-first repository search contract.

## 0.6.0

- Added explicit supervisor receipts and safe candidate review for Team Memory
  and standalone L2 scenarios.
- Added a reproducible `benchmark` command for the bundled fixture and user-
  supplied external benchmark manifests.
- Added the provider protocol manifest/normalization seam without adding a
  runtime dependency on any external memory product.

## 0.3.0

- Bundle a clean, pinned TencentDB Agent Memory source snapshot for the native
  L0-L3 lifecycle and MemoryKnowledge adapter reference.
- Add shared-runtime OpenClaw `before_prompt_build` memory recall with labelled
  layer/status context and no candidate injection.
- Align post-turn capture with upstream sanitization so injected recall and
  assistant code blocks do not feed back into durable memory.
- Remove stale legacy memory tool names from the selected OpenClaw agent's
  active allowlist while keeping the old plugin entries disabled for rollback.

All notable changes to this project are recorded here. The project and its
bundled runtime currently use the same release version. The MCP protocol
revision is a separate compatibility identifier; see
[`docs/compatibility.md`](docs/compatibility.md).

## [Unreleased]

- Keep unreleased changes here until a tagged release is prepared.
- Simplify the public Skill instructions and UI metadata around the real
  doctor -> search -> get workflow, with explicit repository/memory scopes,
  citation handling, and a non-blocking development-tool boundary.
- Group GitHub Actions and Python Dependabot updates so routine maintenance
  opens at most one pull request per ecosystem.
- Add shared Team Memory with explicit publish, context hydration, feedback,
  supersede lifecycle, and reusable decision/failure/discovery/solution/handoff
  records.
- Add a replaceable `TeamMemoryBackend` seam, SQLite WAL/busy-timeout/retry
  behavior, validity-window filtering, stale/wrong lifecycle transitions, and
  explicit portable Team Memory export/import bundles.
- Rename context retrieval from `hybrid-lexical` to the accurate
  `multi-source-lexical`; repository and Team Memory recall run in parallel but
  keep scores and provenance separate.
- Add causal Team Memory revisions (`revision`, `origin_node`,
  `parent_revision`), conflict-aware bundle merge, automatic migration for
  older SQLite databases, and explicit candidate activation after review.
- Extend Team Memory bundles with an append-only revision log for skipped-version
  fast-forward, separate activation reviewer metadata from authorship, and add
  stable feedback IDs for cross-machine deduplication.
- Change the OpenClaw guard to advisory/output-audit behavior; it no longer
  blocks normal file, shell, Git, test, or debugging tools.
- Strengthen evaluator qrels validation, citation commit pinning, and
  multi-gold Recall@5 accounting.
- Add a standard wheel/console entry point and a Windows `msvcrt` snapshot
  lock fallback.
- Separate each doctor memory layer's adapter capability, API readiness, data
  population, and read-back state so supported/reachable layers are never
  reported as populated without records from the layer response.

## [0.2.0] - 2026-08-11

- Added cross-platform GitHub Actions coverage for Python 3.10, 3.12, and
  3.13 on Ubuntu, macOS, and Windows.
- Added a public citation/P@1 regression set with negative-query abstention
  and a deterministic CI gate.
- Unified the project, Skill runtime, installer client, and OpenClaw plugin
  release version at `0.2.0`.
- Added security, contributing, dependency-update, and compatibility
  documentation.
- Kept OpenClaw guard enforcement explicit: audit is the default, enforce is
  opt-in.

## [0.1.0]

- Initial citation-first repository memory runtime, CLI, stdio MCP server,
  optional MemoryCore adapter, and OpenClaw integration.
## 0.7.6

- Add an isolated semantic benchmark A/B selector with truthful effective-mode diagnostics.
- Add a zero-dependency read-only local dashboard (`gui --serve`).
- Record repository index scale metadata and reuse it during large-source routing.
- Add `memory evolve` for explicit L2 projection plus optional supervisor review; L3 remains explicit.
