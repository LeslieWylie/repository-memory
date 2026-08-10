# Write policy

The default path is read-only.

- `feedback` writes a user-level annotation outside every canonical repository.
- `promote` accepts JSON or JSONL and writes a provenance-bearing `candidate/pending` record to the user-level candidate store.
- Search, get, explain, sync, doctor, and MCP calls must not rewrite YAML indexes, Markdown cards, survey sections, reports, or other canonical files.
- Candidates are not visible as verified results until an independent review process accepts them.

Preserve the source, repository, commit, path, line range, and evidence that caused a candidate to be created.

## Automatic post-turn capture

Some hosts can install a lifecycle adapter that invokes the shared runtime
after a successful agent turn. This is an opt-in host integration, not a
behavior that a plain Skill or MCP server can infer on its own. The adapter
must:

- pass only bounded `user` and `assistant` messages;
- drop system, developer, tool, and function messages;
- redact credentials before writing;
- use an idempotency key for retries;
- verify the L0 write before creating anything at L2;
- keep the resulting L2 record `candidate`/`pending` until review;
- never write L3 from the post-turn callback.

The expected state transition is:

```text
agent_end
  -> L0 durable conversation (verified)
  -> L1 extraction (pending or verified)
  -> L2 candidate (pending, derived and reviewable)
  -> explicit human acceptance
  -> L3 profile/core update (verified by read-back)
```

If the native scenario API cannot create a new path, the runtime may keep the
candidate in its user-level derived pending store. That is a safe fallback, not
an accepted native scenario. A candidate must be inspected before promotion;
promotion must read the candidate, write L3, read L3 back, and only then mark
the candidate accepted/archived. Search, ordinary sync, and MCP queries never
perform this promotion.
