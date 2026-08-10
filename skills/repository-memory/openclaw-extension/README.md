# OpenClaw automatic capture

This extension is an adapter, not a second memory store. It listens to the
OpenClaw `agent_end` lifecycle event and invokes the installed
`repository-memory capture-turn` runtime asynchronously.

When `guardEnabled` is on, the host policy has three intent classes:

- `repository-fact`: doctor → search → get, verified citation or abstention;
- `maintenance`: Git inspection, tests, report generation, and explicit writes
  remain available;
- `ordinary`: no repository-memory routing requirement.

The installer sets `enforcement=audit` by default. In audit mode, the extension
records wrong routes, missing doctor/search/get steps, direct fallback, and
incomplete receipts, but does not block the host's tools. Set
`enforcement=enforce` only for a controlled evaluation or compliance run; that
mode blocks bypasses and can revise incomplete answers. This keeps an MCP outage
from deadlocking repair work while preserving an auditable evidence contract.

The write contract is deliberately conservative:

1. user/assistant text is bounded and obvious credentials are redacted;
2. the completed turn is written to MemoryCore L0 and checked back;
3. the native L1 extraction is observed as `verified` or `pending`;
4. durable-looking turns create an L2 `candidate` scenario with provenance;
5. L3 is never written by the hook. Promotion remains an explicit operation.

The extension does not replace an existing OpenClaw memory slot and does not
expose a write MCP tool to the model. Configure `agentIds` when only selected
agents should capture turns.
