# AML submission material

This is the reproducible submission card for the Agent Memory Leaderboard.
It is intentionally separate from the public API key and from any private
evaluation credential.

## Recommended route

Use the academic **code submission / platform deployment** route:

- Public repository: `https://github.com/LeslieWylie/repository-memory`
- Fixed version: `0.7.6`
- Fixed commit: fill this with the release commit after it is pushed
- Docker build:

  ```bash
  docker build -f Dockerfile.aml -t repository-memory-aml .
  ```

- Docker run:

  ```bash
  docker run --rm -p 8080:8080 \
    -e AML_API_KEY="$AML_API_KEY" \
    -v repository-memory-aml-data:/data \
    repository-memory-aml
  ```

- Entrypoint: `repository-memory-aml --host 0.0.0.0 --port 8080`
- Health: `GET /health`
- Add: `POST /add`
- Search: `POST /search`
- Authentication: `Token`, `Bearer`, or `X-Api-Key` when `AML_API_KEY` is set;
  no key is needed for local smoke only.
- Storage: SQLite under `/data`, isolated by a non-reversible `user_id`
  namespace.

## Method disclosure

The submitted service is a standalone, dependency-free implementation. It
stores synchronous Add messages, keeps L0/L1 lifecycle records, and ranks
Search results with a deterministic local character n-gram projection plus
lexical matching and an explicit latest/recent recency tie-breaker. It does not
use private benchmark labels, hidden answers, prompt injection, or benchmark
leakage. L2/L3 promotion is not exposed through the AML Add/Search surface.

The broader repository-memory project also provides Git citation retrieval,
local L0-L3 lifecycle, MCP, CLI, and OpenClaw integration. The AML wrapper is a
narrow competition adapter and does not claim that its local fixture score is a
leaderboard score.

## Local smoke

```bash
XDG_DATA_HOME=/tmp/repository-memory-aml \
  repository-memory-aml --host 127.0.0.1 --port 8080 --api-key local-test-key
```

Then run the curl examples in
[`docs/agent-memory-leaderboard.md`](agent-memory-leaderboard.md).

## Important form boundary

Do not put an Eval Key, a private Memory System Key, or a local/loopback API
address in the public repository. The first-cycle deadline shown by the
official site is August 7, 2026 23:59 (UTC+8); after that date, submission
acceptance and publication are organizer-controlled.
