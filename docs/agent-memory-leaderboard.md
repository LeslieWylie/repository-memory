# Agent Memory Leaderboard integration

The public core includes a dependency-free HTTP wrapper for the AML
Add/Search contract. It is an integration surface, not a local score and not a
claim about leaderboard placement.

## Official contract implemented

- `GET /health` returns 2xx without authentication.
- `POST /add` persists the supplied messages before returning HTTP 200.
- The Add response contains only the declared `success`, `request_id`,
  `user_id`, and `session_id` fields; persistence/read-back is completed before
  the response is sent.
- `POST /search` returns `{"data": [...]}` in relevance order.
- `user_id` is the sole retrieval isolation boundary.
- Message `role` is preserved as any non-empty producer role; the current
  contract does not restrict it to `user` and `assistant`.
- Unix-millisecond source timestamps are normalized to ISO-8601 `created_at`
  values. Explicit latest/recent queries receive a bounded recency tie-breaker;
  ordinary queries remain relevance-first.
- Add responses echo `request_id`, `user_id`, and `session_id` exactly.
- Search returns at most `top_k` items; the formal contract permits 100.
- `Token`, `Bearer`, and `X-Api-Key` authentication are accepted when
  `AML_API_KEY` is configured.

The server stores AML messages as standalone-runtime L0/L1 records in a
hashed user namespace. It does not receive or use benchmark gold answers,
private labels, or hidden evaluation data. L2/L3 promotion remains outside the
participant Add/Search surface.

## Run locally

```bash
XDG_DATA_HOME=/tmp/repository-memory-aml \
  repository-memory-aml --host 127.0.0.1 --port 8080 --api-key local-test-key
```

Smoke it from another terminal:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/add \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: local-test-key' \
  -d '{
    "request_id":"eval:local:add-1",
    "messages":[{"role":"user","content":"I prefer concise technical reports."}],
    "user_id":"local-user",
    "session_id":"local-session"
  }'
curl -fsS http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: local-test-key' \
  -d '{"query":"What report style do I prefer?","user_id":"local-user","top_k":100}'
```

## Docker code-submission path

```bash
docker build -f Dockerfile.aml -t repository-memory-aml .
docker run --rm -p 8080:8080 \
  -e AML_API_KEY="$AML_API_KEY" \
  -v repository-memory-aml-data:/data \
  repository-memory-aml
```

The public challenge route requires the maintainer to submit this repository,
the Docker command, the endpoint paths, the public method description and any
required attribution through the official evaluation request. The platform
must deploy the service and run its own smoke/full evaluation; local fixture
scores are not leaderboard results.

For the form, choose the academic **code submission** route when available. It
uses the public GitHub repository and Docker entrypoint, so it does not require
putting a local API address or an Eval Key in the form. The exact material is
listed in [`docs/aml-submission.md`](aml-submission.md).

## Local verification

```bash
PYTHONPATH=skills/repository-memory/scripts \
  python3 -m unittest skills/repository-memory/tests/test_aml_server.py -v
```

No API key, benchmark data, or private evaluation artifact belongs in this
repository.
