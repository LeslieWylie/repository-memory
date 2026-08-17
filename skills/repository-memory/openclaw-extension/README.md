# OpenClaw automatic recall and capture

This extension is an adapter, not a second memory store. It listens to the
OpenClaw `before_prompt_build` and `agent_end` lifecycle events invoke the
installed shared `repository-memory` runtime. The first hook performs bounded
`scope=memory` recall; the second invokes `capture-turn` asynchronously.

When the host exposes `api.registerTool`, the extension also registers four
native read-only OpenClaw tools:

- `repository_memory_doctor`
- `repository_memory_search`
- `repository_memory_get`
- `repository_memory_timeline`

They delegate to the same CLI runtime as MCP, so the plugin does not create a
second ranking or storage implementation. The extension also observes
`session_start`, `session_end`, `tool_result_persist`, `before_tool_call`, and
`after_tool_call`. These hooks write metadata-only audit records; they never
promote memory or block ordinary tools.

This follows the useful part of TencentDB's client plugin: L1 search, L2
scenario navigation, and L3 profile context are recalled before the model turn;
L0/L1/L2/L3 stay labelled and are never converted into repository citations.
The Python runtime remains the only ranking and normalization implementation.

When `guardEnabled` is on, the host policy has three intent classes:

- `repository-fact`: prefer doctor → context/search → get, verified citation or
  an explicit uncertainty statement;
- `maintenance`: Git inspection, tests, report generation, and explicit writes
  remain available;
- `ordinary`: no repository-memory routing requirement.

The installer sets `enforcement=audit` by default. The extension records wrong
routes, missing doctor/search/get steps, direct fallback, and incomplete
receipts, but does not block the host's tools in either mode. The old
`enforcement=enforce` value is retained for configuration compatibility; it is
not a shell sandbox and cannot deadlock repair work.

The write contract is deliberately conservative:

1. user/assistant text is bounded and obvious credentials are redacted;
2. the completed turn is written to MemoryCore L0 and checked back;
3. the native L1 extraction is observed as `verified` or `pending`;
4. durable-looking turns create an L2 `candidate` scenario with provenance;
5. L3 is never written by the hook. Promotion remains an explicit operation.

The extension does not replace an existing OpenClaw memory slot and does not
expose a write MCP tool to the model. Configure `agentIds` when only selected
agents should capture turns. Set `nativeTools: false` only for hosts without
native tool registration; the namespaced stdio MCP remains available.

## Verify the OpenClaw path

After installation, inspect the plugin and MCP registration, then run one
positive and one fabricated negative query through the selected agent profile:

```bash
openclaw plugins inspect repository-memory-autocapture --json
openclaw mcp probe repository-memory --json
repository-memory doctor --json
```

The result must show the actual runtime, source commit, retrieval mode, and
memory population. A ready endpoint alone is not evidence that L2/L3 contain
useful records.
