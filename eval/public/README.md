# Public retrieval regression set

This is a small, privacy-free benchmark over this repository's own public
documentation and runtime contract. It exists to catch accidental changes to
top-1 citation routing, negative-query abstention, and citation validity.

The qrels are document-level stable IDs in the form
`<source-id>:<relative-path>`. They intentionally use distinctive terms and
do not claim to measure semantic understanding. `strict_precision_at_1` is the
release gate; MRR and Recall are diagnostic only.

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
