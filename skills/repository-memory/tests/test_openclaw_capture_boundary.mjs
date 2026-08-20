import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

// The pending-capture queue writes under the home directory.  Point HOME at a
// temporary tree before the plugin loads so this test never touches the real
// autocapture state.
const home = await mkdtemp(join(tmpdir(), "repository-memory-capture-home-"));
process.env.HOME = home;

const plugin = (await import("../openclaw-extension/index.mjs")).default;

const hooks = new Map();
const auditDir = await mkdtemp(join(tmpdir(), "repository-memory-capture-audit-"));
const api = {
  pluginConfig: {
    enabled: true,
    guardEnabled: false,
    recallEnabled: false,
    agentIds: ["yaole"],
    auditPath: join(auditDir, "audit.jsonl"),
    // Forcing the runtime to fail routes the turn into the pending queue, which
    // is the only place the exact capture payload is observable from a test.
    runtime: "does-not-run-in-this-test",
  },
  logger: { info() {}, warn() {} },
  on(name, handler) {
    if (!hooks.has(name)) hooks.set(name, []);
    hooks.get(name).push(handler);
  },
};

plugin.register(api);

async function fire(name, event, ctx) {
  for (const handler of hooks.get(name) || []) await handler(event, ctx);
}

const ctx = { agentId: "yaole", sessionKey: "session-capture-boundary" };
const RAW = "记一下今天的决定：agent_end 自动记忆只对 yaole 启用。标记 CAPTUREBOUNDARYTEST";
// Reproduces how the host hands the prompt to `before_agent_run`: another
// plugin's `prependContext` is already merged into the user turn.
const INJECTED =
  "## Memory system — ACTION REQUIRED\n\n" +
  "Auto-recall found no relevant results for a long query. You MUST call `memory_search` now.\n\n" +
  RAW;

await fire("before_prompt_build", { prompt: RAW, messages: [] }, ctx);
await fire("before_agent_run", { prompt: INJECTED, messages: [] }, ctx);
await fire(
  "agent_end",
  {
    success: true,
    messages: [
      { role: "user", content: INJECTED },
      { role: "assistant", content: "记住了。标记 CAPTUREBOUNDARYTEST" },
    ],
  },
  ctx,
);

const pendingDir = join(home, ".local", "share", "repository-memory", "autocapture", "pending");
let entries = [];
for (let attempt = 0; attempt < 100 && entries.length === 0; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 100));
  entries = await readdir(pendingDir).catch(() => []);
}
assert.equal(entries.length, 1, "a failed capture must queue exactly one pending record");

const queued = JSON.parse(await readFile(join(pendingDir, entries[0]), "utf8"));
const payload = queued.payload;

// The turn boundary must carry the text the user actually typed.  Keeping the
// post-prompt-build value would sediment another plugin's injected directive
// into L0 as if it were part of the user's turn.
assert.equal(payload.original_user_text, RAW);
assert.ok(!payload.original_user_text.includes("ACTION REQUIRED"));
assert.equal(payload.session_id, "session-capture-boundary");
assert.ok(payload.messages.some((message) => message.role === "assistant"));

console.log("openclaw capture boundary ok");
