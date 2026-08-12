# Security policy

Repository Memory handles source citations, local indexes, optional
conversation memory, and host integration. It must not receive credentials,
private keys, tokens, or complete private conversations through an issue or
pull request.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when it is
available. If private reporting is not available, open a minimal issue that
contains no secrets, exploit payload, or private data and ask for a private
contact channel.

Include the affected version or commit, operating system, reproduction steps,
impact, and any safe redacted logs. Do not publish credentials or real user
conversation data.

## Security boundaries

- Search and sync must not modify a canonical repository.
- Credentials belong in user configuration or the host secret store, never in
  Git, examples, fixtures, or audit logs.
- `verified` requires a citation that can be checked; candidates are not
  facts.
- The OpenClaw guard is audit-first by default. Enforcement is an explicit
  deployment choice and is tested separately.
- Report suspected dependency or GitHub Action supply-chain issues with the
  same redaction rules.
