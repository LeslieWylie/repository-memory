# Citation contract

Attach citations to the claims they support. A usable citation contains:

- source id and repository;
- commit and commit type;
- source path;
- line_start and line_end;
- excerpt or a `get`-resolvable source record;
- evidence status and generated status;
- freshness and validation state.

Before making a strong claim, use `get` or `explain` when the search excerpt is not sufficient. Pass the search citation's commit to pin the follow-up read; a commit mismatch must be treated as stale and trigger a new search. A citation is not verified merely because a path exists: the path, line range, and excerpt must resolve in the cited source snapshot, and the commit must match the indexed snapshot. Dirty, stale, generated, pending, and inferred material must be qualified or treated as a candidate.
