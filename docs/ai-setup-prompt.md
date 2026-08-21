# AI setup prompt

A complete, self-contained prompt for handing to an AI operator on a fresh
host. It installs the runtime, verifies it honestly, and teaches the usage
contract. Replace the angle-bracket placeholders; keep everything else —
every instruction below exists because a real install tripped without it.
Teams can bake their own defaults (embedding endpoint, team repository,
credential names) into a wrapper and shorten step 1 to one sentence; keep
organization URLs in that wrapper, not in copies of this prompt.

```text
Install and live-test the repository-memory team knowledge base. Treat only
observed command output as evidence — this prompt is not proof of anything.

[Prerequisites] python3 >= 3.10 and git; network to github.com. The runtime
has zero third-party dependencies and never downloads a model. Everything
writes user directories only; it never modifies your working tree or the
knowledge repository itself.

[Step 1 — install]
cd into the Git repository that should become the knowledge source, then:

curl -fsSL https://raw.githubusercontent.com/LeslieWylie/repository-memory/main/bootstrap.sh | sh -s -- \
  --target auto \
  --source-url "<canonical HTTPS url of this repository>" \
  --source-branch "$(git branch --show-current)" \
  --json

Optional planes, same command: --cjk (jieba; a PEP 668 refusal is reported,
not fatal), --semantic-provider gateway --semantic-model <model>
--semantic-endpoint <openai-compatible /v1> --semantic-dimensions <n>
--semantic-api-key-env <VAR_NAME> (only the credential's NAME is ever
persisted), --team-repository <team repo HTTPS url> --team-agent-id
<globally meaningful node name — never something like "main">.

Known traps, all measured on real hosts:
- --source-url must be canonical HTTPS. `git config --get remote.origin.url`
  may return an SSH alias from ~/.ssh/config that resolves nowhere else.
- HTTPS needs a non-interactive credential: netrc, a credential helper, or
  `gh auth setup-git` (old gh releases lack `gh auth token`; setup-git still
  works).
- A host with OpenClaw needs --openclaw-agent <id>, and --openclaw-config
  <path> when the config is not at ~/.openclaw.
- Installing rewrites MCP registrations: a session that was already open
  loses its MCP until the next session. That is documented behavior, not a
  failure.

[Step 2 — verify, no skipping, no inference]
1. In the install JSON, verification.doctor.status and
   verification.mcp.status must both be "ready". Anything else: report the
   raw error and stop claiming success.
2. Positive probe: pick a specific term that exists only in this corpus
   (from its README or a recent commit) and ask in one natural sentence:
     ~/.local/bin/repository-memory search "<that sentence>" --json
   Require abstain=false and results[0].citation with path/line_start/
   line_end/commit. Report path:lines@commit and the excerpt.
   If it abstains: read the candidates' support.unmatched_terms — they name
   the words the corpus lacks — re-ask once in the document's own
   vocabulary. Never pad a retry with invented specifics.
3. Negative probe:
     ~/.local/bin/repository-memory search "ZZZQWE fabricated project recent progress" --json
   Require abstain=true and verified=0. A memory that invents answers is
   worse than none; if this does not abstain, that is a bug to report.
4. If a team repository was configured: team-status --json must show the
   team plane ready, and the install receipt's team.publish_cron line should
   be installed with crontab (report `crontab -l` output). Never run
   `supervise` from automation — review is an explicit human-triggered step.
5. If your host registered MCP (Claude Code / Codex): in the NEXT session,
   call the repository-memory memory_search tool with the same positive
   probe and confirm it matches the CLI answer.

[Step 3 — the usage contract]
For any question about this project or team — what happened, who did what
and when, why something was decided, what the team already knows — call
memory_search with the user's words verbatim. Do not rewrite into filenames,
do not pre-select a scope, do not call doctor first. Read answers from
results/answerable only. abstain=true means the stores hold no evidence:
say so. Check answered_by before giving up — groups.memory and groups.team
may answer when the repository plane cannot, and their content must be
labelled as conversation memory or a team decision, never as a Git citation.
citation.pinned=false means the evidence was verified against a working tree
rather than a commit; attribute it accordingly.

[Step 4 — report]
Install JSON summary (doctor/mcp status, source id and root, cjk/semantic/
team fields) + positive citation (path:lines@commit + excerpt) + negative
abstention + every raw error encountered. Credential NAMES may appear in the
report; credential VALUES never.
```
