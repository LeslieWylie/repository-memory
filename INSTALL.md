# Install

One runtime, four doors. This page is the per-host install guide; the
[quickstart](docs/quickstart.md) covers first queries and team workflows, and
[README_CN.md](README_CN.md) is the Chinese overview.

Requirements: `python3` ≥ 3.10 and `git`. The runtime has zero third-party
dependencies and never downloads a model. Everything below writes only user
directories (`~/.cache`, `~/.local`, `~/.claude`, `~/.codex`, `~/.openclaw`);
it never modifies your working tree or the knowledge repository itself.

## One command on a fresh host

Run inside the Git repository you want as the knowledge source:

```bash
curl -fsSL https://raw.githubusercontent.com/LeslieWylie/repository-memory/main/bootstrap.sh | sh -s -- \
  --target auto \
  --source-url "<canonical HTTPS url of this repository>" \
  --source-branch "$(git branch --show-current)" \
  --json
```

The install output is the receipt: `verification.doctor.status` and
`verification.mcp.status` must both be `"ready"`. Anything else is not
installed, whatever the rest of the output says.

Not on PyPI. From a local checkout, `python3 -m pip install
/path/to/repository-memory` or `python3 install.py --target auto --json`.

## Per host

- **Claude Code / Codex** — `--target auto` installs the Skill into
  `~/.claude/skills` / `~/.codex/skills` and registers the audited MCP server
  (`repository-memory`, stdio, protocol `2026-07-28` with fallback to
  `2025-11-25` / `2025-06-18` / `2025-03-26` / `2024-11-05`). A session that
  was already open keeps its old MCP connection until the next session.
- **OpenClaw** — the installer refuses a bare `--target auto` until you name
  the agent: add `--openclaw-agent <id>` (or `--openclaw-all-agents` when that
  is intentional). A non-default config directory needs
  `--openclaw-config <path>`, or the install lands in an empty `~/.openclaw`
  and reports `agent id not found`. Installs the native
  `repository_memory_*` tools plus the lifecycle capture extension.
- **Any other harness** — spawn the stdio MCP (`repository-memory mcp`) or
  call the CLI directly: `~/.local/bin/repository-memory search|get|doctor
  --json`. Same runtime, same result contract.

## Install traps, all measured on real fresh hosts

- `--source-url` must be a canonical HTTPS URL. `git config --get
  remote.origin.url` may return an SSH host alias from the operator's
  `~/.ssh/config` (e.g. `code.example.cn-alice:...`), which resolves nowhere
  else.
- HTTPS needs a credential the host can use non-interactively: netrc, a
  credential helper, or `gh auth setup-git` (old `gh` releases such as 2.4.0
  have no `gh auth token`, but `setup-git` still wires the helper).
- The team repository clone needs a git identity before the first publish;
  `team-publish` preflights this and answers `missing_git_identity` with the
  exact commands.
- Give agents globally meaningful ids. Team-memory inbox directories group by
  the *capturing agent's* id carried in each record — a host whose agent is
  literally named `main` publishes into `inbox/main/`, where another node's
  `main` would mix with it.

## Verify like you mean it

```bash
~/.local/bin/repository-memory --version
~/.local/bin/repository-memory doctor --json
~/.local/bin/repository-memory search "<a question only this corpus can answer>" --json
~/.local/bin/repository-memory search "ZZZQWE fabricated project recent progress" --json
```

The positive must return `abstain=false` with a citation carrying
path/line/commit; the fabricated negative must return `abstain=true` with
`verified=0`. A memory that invents answers is worse than none.

## Team memory loop (optional, multi-agent)

```bash
git clone <team knowledge repository> ~/team-knowledge-data
repository-memory team-configure --repository ~/team-knowledge-data --agent-id <node-agent-id> --json
repository-memory team-sync --json
```

Schedule the publish loop per node (pull → sync → commit `knowledge/` → push;
review deliberately stays a separate explicit step):

```text
41 19 * * * ~/.local/bin/repository-memory team-publish --json >> ~/team-publish.log 2>&1
```

## Exit codes

`0` success; `1` a gate failed (`evaluate`/`team-evaluate --gate`); `2` the
command errored — the JSON body on stdout carries the reason. Stdout is always
UTF-8 regardless of the console code page.
