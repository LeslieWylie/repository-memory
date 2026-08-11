# Contributing

Repository Memory is a citation-first runtime. Changes should preserve the
separation between canonical sources and derived indexes, and should never
turn a related or stale result into a verified fact.

## Development setup

Python 3.10 or newer and Git are required. Node.js 22 or newer is needed for
the OpenClaw extension checks.

The project also builds a standard wheel and installs a cross-platform
`repository-memory` console command:

```bash
python -m pip install .
repository-memory --help
```

```bash
python -m pip install pytest pytest-cov
python -m pytest -q --cov=skills/repository-memory/scripts --cov-branch
node --check skills/repository-memory/openclaw-extension/index.mjs
node skills/repository-memory/tests/test_openclaw_guard.mjs
python skills/repository-memory/scripts/eval_gate.py \
  --root . --queries eval/public/queries.jsonl \
  --qrels eval/public/qrels.jsonl --revision HEAD --local
```

## Change rules

1. Add or update a focused test for behavior changes.
2. Preserve citation validation, source/commit/path/line provenance, and
   explicit abstention for unsupported claims.
3. Keep adapters behind the runtime seam; do not duplicate retrieval logic in
   the CLI, MCP server, or host plugin.
4. Do not commit private repositories, generated indexes, credentials, audit
   logs, or host configuration.
5. For retrieval changes, update the public qrels only when the gold choice
   is genuinely wrong; explain the decision in the qrel's `reason` field.
6. Document compatibility-impacting changes in `CHANGELOG.md`.

Pull requests should state the tested operating systems, Python/Node
versions, whether the public regression gate was run, and whether the wheel
smoke test was run.
