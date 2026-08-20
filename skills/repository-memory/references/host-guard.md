# Host routing and bypass guard

The Skill defines the evidence contract; a host integration enforces it. The
host must distinguish the repository-memory MCP tools from any built-in memory
tool with a similar name.

For a repository-fact request, the preferred sequence is:

```text
memory_search → memory_get
```

`memory_search` defaults to `scope="auto"`, which recalls repository evidence,
conversation memory, and Team Memory in their own planes and returns them as
separate groups. A `memory_doctor` call is not a required preamble: ask first,
and call doctor/sync only when the response reports the source as
`not_configured` or stale.

When the host adds a namespace to MCP names, use that namespaced name. A bare
`memory_search` is not interchangeable with the repository-memory server.

The guard keeps two configuration values for compatibility, but its memory
policy is advisory in both modes:

- `audit` (default): record routing/citation observations and continue;
- `enforce`: retain the label for old host configurations, but do not turn this
  plugin into a capability sandbox. Controlled evaluation should validate the
  final evidence receipt, not block `read`, `grep`, `git`, `exec`, tests, or
  debugging.

In either mode it should:

- observe whether a doctor result preceded repository search;
- record a bare host memory search as a routing warning;
- keep three policies separate: `repository-fact`, `maintenance`, and
  `ordinary`;
- audit high-confidence direct file reads or source-reading shell commands for
  `repository-fact` requests, without blocking them;
- leave maintenance work such as `git log` plus an explicitly requested report
  write available. A word such as “提交记录” is a fact-query noun, not a
  maintenance permission;
- record recovery attempts after a failed search, while leaving host-level
  command permissions to the host;
- record only tool name, outcome, scope, result counts, citation counts,
  freshness, and latency;
- let a query with no verified evidence finish as an explicit abstention.

The guard is not a general shell sandbox and does not attempt to classify every
possible script as a file read. In both modes it is an
observability/output-validation layer. Hosts should expose ordinary tool
permission and destructive-operation controls separately.

Host enforcement is not portable by implication. If a host has no tool-call
hook, it can still use the Skill and MCP, but its configuration must not claim
that direct shell/file access is blocked.
