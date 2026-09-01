# Local vector database prototype

This is an isolated, reversible benchmark for storing Repository Memory's
derived semantic chunks in a persistent local Qdrant database. It does not
replace the canonical Git source, lexical answerability checks, or citation
validation.

The prototype:

- discovers the newest compatible `*.semantic.json` cache by default;
- keeps the database under
  `~/.local/share/repository-memory/vector-db/qdrant`;
- synchronizes changed/new chunk vectors and deletes stale chunk IDs;
- compares Qdrant retrieval with the current Python exact scan and a NumPy
  exact-scan baseline;
- verifies an insert/update/delete cycle in a temporary collection;
- writes a reproducible JSON report beside the database.

Install the prototype-only dependency and run it from the repository root:

```bash
uv pip install --python .venv/bin/python -r experiments/local-vector-db/requirements.txt
.venv/bin/python experiments/local-vector-db/benchmark_qdrant.py
```

Pass `--metadata /absolute/path/to/index.semantic.json` to select another
repository cache, or `--iterations 20` for a longer latency run. A second run
against unchanged metadata should report a no-op sync.

This is a prototype, not a production-ready backend. Embedded Qdrant local
mode is useful for measuring persistence and vector-store behavior without a
daemon. If a later workload requires concurrent writers, remote clients, or a
large corpus, the same collection contract should be tested against a Qdrant
server before integration.
