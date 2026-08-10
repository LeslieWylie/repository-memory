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

The guard should:

- require a doctor result before the first repository search in a run;
- block a bare host memory search for repository-fact requests;
- block direct file and shell fallback after a missing, failed, stale, or
  citation-invalid repository result;
- leave ordinary coding, testing, and explicitly requested file operations
  available outside a repository-fact request;
- record only tool name, outcome, scope, result counts, citation counts,
  freshness, and latency;
- let a query with no verified evidence finish as an explicit abstention.

Host enforcement is not portable by implication. If a host has no tool-call
hook, it can still use the Skill and MCP, but its configuration must not claim
that direct shell/file access is blocked.
