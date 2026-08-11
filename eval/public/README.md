# Public retrieval regression set

This is a small, privacy-free benchmark over this repository's own public
documentation and runtime contract. It exists to catch accidental changes to
top-1 citation routing, negative-query abstention, and citation validity.

The qrels are document-level stable IDs in the form
`<source-id>:<relative-path>`. They intentionally use distinctive terms and
do not claim to measure semantic understanding. `strict_precision_at_1` and
`recall_at_5` are release gates; MRR is diagnostic. P@1 is the fraction of
positive queries whose first **verified** result is a positive qrel. Recall@5
is the macro average of retrieved positive qrels divided by all positive
qrels for each query; the report also emits the micro numerator/denominator.
Candidates, stale results, and abstentions never count as hits.

Run it from the repository root:

```bash
python skills/repository-memory/scripts/eval_gate.py \
  --root . \
  --queries eval/public/queries.jsonl \
  --qrels eval/public/qrels.jsonl \
  --revision HEAD \
  --local
```

The set contains no private repository names, credentials, user data, or
organization-specific labels. Add a new query and qrel together, explain the
gold choice in the qrel's `reason`, and run the gate before changing a
retrieval heuristic.

The synthetic Team Memory fixture is separate from repository qrels. Run it
with:

```bash
python skills/repository-memory/scripts/repository-memory.py team-evaluate \
  --records eval/public/team_memory/records.jsonl \
  --queries eval/public/team_memory/queries.jsonl \
  --qrels eval/public/team_memory/qrels.jsonl --json
```

It measures Team Memory P@1, MRR@5, Recall@5, negative abstention, candidate
contamination, and latency in a temporary database. It does not use or modify
the user-level Team Memory store.
