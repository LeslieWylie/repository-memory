# Host routing and bypass guard

The Skill defines the evidence contract; a host integration enforces it. The
host must distinguish the repository-memory MCP tools from any built-in memory
tool with a similar name.

For a repository-fact request, the preferred sequence is:

```text
memory_doctor → memory_search(scope=repository) → memory_get
```

When the host adds a namespace to MCP names, use that namespaced name. A bare
`memory_search` is not interchangeable with the repository-memory server.

The guard should support two enforcement modes:

- `audit` (default): record routing/citation violations and continue, so a
  backend outage cannot deadlock diagnostics or ordinary work;
- `enforce`: block bypasses and request answer revision for controlled
  evaluation/compliance runs.

In either mode it should:

- require a doctor result before the first repository search in a run;
- block a bare host memory search for repository-fact requests;
- keep three policies separate: `repository-fact`, `maintenance`, and
  `ordinary`;
- in `enforce`, block only high-confidence direct file reads or source-reading
  shell commands for `repository-fact` requests. Generic `exec`, tests,
  builds, `git status`, patch tools, and search tools remain available;
- leave maintenance work such as `git log` plus an explicitly requested report
  write available. A word such as “提交记录” is a fact-query noun, not a
  maintenance permission;
- in `enforce`, after a failed repository search, allow only a narrow, non-destructive
  diagnostic/recovery command for the repository-memory runtime. File reads,
  arbitrary shell, destructive Git, and deletion commands remain blocked;
- record only tool name, outcome, scope, result counts, citation counts,
  freshness, and latency;
- let a query with no verified evidence finish as an explicit abstention.

The guard is not a general shell sandbox and does not attempt to classify every
possible script as a file read. In audit mode it is an observability layer; in
enforce mode it is a narrow routing guard. Hosts should still expose
ordinary tool permission controls separately.

Host enforcement is not portable by implication. If a host has no tool-call
hook, it can still use the Skill and MCP, but its configuration must not claim
that direct shell/file access is blocked.
