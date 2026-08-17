import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import plugin from "../openclaw-extension/index.mjs";

const hooks = new Map();
const auditDir = await mkdtemp(join(tmpdir(), "repository-memory-guard-test-"));
const api = {
  pluginConfig: {
    enabled: true,
    guardEnabled: true,
    enforcement: "enforce",
    agentIds: ["yaole"],
    auditPath: join(auditDir, "audit.jsonl"),
    runtime: "does-not-run-in-this-test",
  },
  logger: { info() {}, warn() {} },
  on(name, handler) {
    hooks.set(name, handler);
  },
};

plugin.register(api);
const ctx = { agentId: "yaole", sessionKey: "session-1" };
const promptEvent = { prompt: "上次评测结果什么情况" };
await hooks.get("before_agent_run")(promptEvent, ctx);

const bare = await hooks.get("before_tool_call")({ toolName: "memory_search", params: { query: "eval" }, runId: "run-1" }, ctx);
assert.equal(bare, undefined);

const beforeDoctor = await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" }, runId: "run-1" }, ctx);
assert.equal(beforeDoctor, undefined);

const doctor = await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_doctor", params: {}, runId: "run-1" }, ctx);
assert.equal(doctor, undefined);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_doctor",
  result: { status: "ready" },
  runId: "run-1",
}, ctx);

const search = await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" }, runId: "run-1" }, ctx);
assert.equal(search, undefined);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: { content: [{ type: "text", text: JSON.stringify({ verified: [{ citation: { valid: true } }], candidates: [], abstain: false, freshness: { source: { state: "fresh" } } }) }] },
  runId: "run-1",
}, ctx);

const direct = await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "cat report.md" }, runId: "run-1" }, ctx);
assert.equal(direct, undefined);

const get = await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_get", params: { id: "source:report.md" }, runId: "run-1" }, ctx);
assert.equal(get, undefined);
const finalize = await hooks.get("before_agent_finalize")({ runId: "run-1" }, ctx);
assert.equal(finalize, undefined);

// The gateway may omit session/run fields on a later hook.  Agent identity
// fallback must preserve the same policy state in that case.
const driftCtx = { agentId: "yaole", sessionKey: "session-3" };
await hooks.get("before_agent_run")({ prompt: "最近的评测结果" }, driftCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_doctor", params: {} }, { agentId: "yaole" });
await hooks.get("after_tool_call")({ toolName: "repository-memory__memory_doctor", result: { status: "ready" } }, { agentId: "yaole" });
const driftSearch = await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" } }, { agentId: "yaole" });
assert.equal(driftSearch, undefined);

const ordinaryCtx = { agentId: "yaole", sessionKey: "session-2" };
await hooks.get("before_agent_run")({ prompt: "修复测试失败并运行命令" }, ordinaryCtx);
const ordinaryBare = await hooks.get("before_tool_call")({ toolName: "memory_search", params: { query: "general memory" }, runId: "run-2" }, ordinaryCtx);
assert.equal(ordinaryBare, undefined);
const ordinaryExec = await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "pytest" }, runId: "run-2" }, ordinaryCtx);
assert.equal(ordinaryExec, undefined);

// A noun such as "提交记录" must not turn a fact query into a maintenance
// turn and thereby create a direct-file bypass.
const commitFactCtx = { agentId: "yaole", sessionKey: "commit-fact-1" };
await hooks.get("before_agent_run")({ prompt: "查询最近的提交记录和评测结果" }, commitFactCtx);
const commitFactExec = await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "git show HEAD" } }, commitFactCtx);
assert.equal(commitFactExec, undefined);
const alternateRead = await hooks.get("before_tool_call")({ toolName: "filesystem.readFile", params: { path: "report.md" } }, commitFactCtx);
assert.equal(alternateRead, undefined);
const ordinaryExecDuringFact = await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "pytest -q" } }, commitFactCtx);
assert.equal(ordinaryExecDuringFact, undefined);
const statusDuringFact = await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "git status --short" } }, commitFactCtx);
assert.equal(statusDuringFact, undefined);
const searchToolIsNotAFileRead = await hooks.get("before_tool_call")({ toolName: "file_search", params: { query: "report" } }, commitFactCtx);
assert.equal(searchToolIsNotAFileRead, undefined);
const recentRuntimeFactCtx = { agentId: "yaole", sessionKey: "recent-runtime-fact-1" };
await hooks.get("before_agent_run")({ prompt: "查看最近运行结果" }, recentRuntimeFactCtx);
const recentRuntimeRead = await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "cat report.md" } }, recentRuntimeFactCtx);
assert.equal(recentRuntimeRead, undefined);

const receiptCtx = { agentId: "yaole", sessionKey: "session-4" };
await hooks.get("before_agent_run")({ prompt: "查询评测结果" }, receiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_doctor", params: {} }, receiptCtx);
await hooks.get("after_tool_call")({ toolName: "repository-memory__memory_doctor", result: { status: "ready" } }, receiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" } }, receiptCtx);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: { verified: [{ citation: { valid: true } }], candidates: [], abstain: false, freshness: { state: "fresh" } },
}, receiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_get", params: { id: "source:eval.md" } }, receiptCtx);
const missingReceipt = await hooks.get("before_agent_finalize")({ finalText: "评测结果是 90%" }, receiptCtx);
assert.equal(missingReceipt, undefined);

// A maintenance task may legitimately inspect Git and write a report.  It is
// not a repository-fact answer and must not deadlock behind the evidence guard.
const maintenanceCtx = { agentId: "yaole", sessionKey: "maintenance-1" };
await hooks.get("before_agent_run")({ prompt: "根据 commit 记录把日报交了" }, maintenanceCtx);
assert.equal(await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "git log --oneline -10" } }, maintenanceCtx), undefined);
assert.equal(await hooks.get("before_tool_call")({ toolName: "write", params: { path: "standup.md", content: "report" } }, maintenanceCtx), undefined);

// If the repository MCP itself fails, record recovery attempts without
// turning the memory extension into a shell sandbox.
const recoveryCtx = { agentId: "yaole", sessionKey: "recovery-1" };
await hooks.get("before_agent_run")({ prompt: "上次评测结果什么情况" }, recoveryCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_doctor", params: {} }, recoveryCtx);
await hooks.get("after_tool_call")({ toolName: "repository-memory__memory_doctor", result: { status: "ready" } }, recoveryCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" } }, recoveryCtx);
await hooks.get("after_tool_call")({ toolName: "repository-memory__memory_search", error: "timeout", result: { ok: false, error: "timeout" } }, recoveryCtx);
assert.equal(await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "repository-memory doctor --json" } }, recoveryCtx), undefined);
const unsafeRecovery = await hooks.get("before_tool_call")({ toolName: "exec", params: { command: "repository-memory doctor --json; rm -rf /tmp/x" } }, recoveryCtx);
assert.equal(unsafeRecovery, undefined);
const fileFallback = await hooks.get("before_tool_call")({ toolName: "read", params: { path: "report.md" } }, recoveryCtx);
assert.equal(fileFallback, undefined);

// A weak answer containing only the word "source" is not a valid receipt.
const weakReceiptCtx = { agentId: "yaole", sessionKey: "weak-receipt-1" };
await hooks.get("before_agent_run")({ prompt: "查询评测结果" }, weakReceiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_doctor", params: {} }, weakReceiptCtx);
await hooks.get("after_tool_call")({ toolName: "repository-memory__memory_doctor", result: { status: "ready" } }, weakReceiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" } }, weakReceiptCtx);
await hooks.get("after_tool_call")({ toolName: "repository-memory__memory_search", result: { verified: [{ citation: { valid: true } }], abstain: false, freshness: { state: "fresh" } } }, weakReceiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_get", params: { id: "source:eval.md" } }, weakReceiptCtx);
const weakReceipt = await hooks.get("before_agent_finalize")({ finalText: "来源是评测报告，结果是 90%" }, weakReceiptCtx);
assert.equal(weakReceipt, undefined);

// A real citation can still be insufficient for the complete compound claim.
// The guard must observe that distinction and record a claim-level abstention;
// it remains advisory and does not block ordinary host tools.
const partialCtx = { agentId: "yaole", sessionKey: "partial-claim-1" };
await hooks.get("before_agent_run")({ prompt: "查询复合评测结果" }, partialCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_doctor", params: {} }, partialCtx);
await hooks.get("after_tool_call")({ toolName: "repository-memory__memory_doctor", result: { status: "ready" } }, partialCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "复合评测结果" } }, partialCtx);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: { verified: [{ citation: { valid: true } }], answerable: [], candidates: [], abstain: true, claim_abstain: true, freshness: { state: "fresh" } },
}, partialCtx);
await hooks.get("before_agent_finalize")({ finalText: "评测结果是 90%，可以直接下结论" }, partialCtx);

// Operational default: audit routing/citation violations without deadlocking
// useful diagnostics when a backend is slow or incomplete.
const auditCtx = { agentId: "yaole", sessionKey: "audit-1" };
const auditHooks = new Map();
const auditApi = {
  pluginConfig: {
    enabled: true,
    guardEnabled: true,
    enforcement: "audit",
    agentIds: ["yaole"],
    auditPath: join(auditDir, "audit-mode.jsonl"),
  },
  logger: { info() {}, warn() {} },
  on(name, handler) { auditHooks.set(name, handler); },
};
plugin.register(auditApi);
await auditHooks.get("before_agent_run")({ prompt: "查询评测结果" }, auditCtx);
assert.equal(await auditHooks.get("before_tool_call")({ toolName: "exec", params: { command: "cat report.md" } }, auditCtx), undefined);
assert.equal(await auditHooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" } }, auditCtx), undefined);
const auditFinalize = await auditHooks.get("before_agent_finalize")({ finalText: "结果待核实" }, auditCtx);
assert.equal(auditFinalize, undefined);

const audit = await readFile(join(auditDir, "audit.jsonl"), "utf8");
assert.doesNotMatch(audit, /tool_blocked/);
assert.match(audit, /tool_completed/);
assert.match(audit, /result_shape/);
assert.match(audit, /"verified":1/);
assert.match(audit, /"answerable":0/);
assert.match(audit, /claim_abstain/);
assert.match(audit, /"citations":1/);
assert.match(audit, /recovery_allowed/);
const auditMode = await readFile(join(auditDir, "audit-mode.jsonl"), "utf8");
assert.match(auditMode, /tool_audited/);

// The TencentDB-compatible lifecycle path must use the shared runtime and
// inject only answerable memory records, never candidates.
//
// This fixture has to be a real spawnable "runtime", not a stub, because it
// exercises runRecall()'s actual spawn() call, which passes a fixed argv
// (["search", prompt, "--scope", ...]) and no shell option. A POSIX kernel
// dispatches a `#!/bin/sh` shebang directly at exec() time -- no shell
// involved, so a script file with the exec bit set is enough there. Windows
// has no shebang dispatch, and (since the CVE-2024-27980 fix) spawn() without
// shell:true refuses to CreateProcess a .cmd/.bat file at all (EINVAL) -- so
// on Windows the fixture points runtime at the real node.exe (a genuine PE
// binary, spawnable with no shell) and forces it to print the payload and
// exit via a --require preload module injected through NODE_OPTIONS; preload
// modules run before entry-point resolution, so node.exe never gets as far
// as trying (and failing) to resolve the fixed "search" argv as a script.
const RECALL_PAYLOAD = JSON.stringify({
  groups: {
    memory: {
      verified: [{ id: "memorycore:L3:profile", status: "accepted" }],
      answerable: [{
        id: "memorycore:L3:profile",
        memory_layer: "L3",
        status: "accepted",
        content: "accepted profile context",
        citation: { valid: true },
      }],
      candidates: [{ status: "candidate", content: "do not inject" }],
    },
  },
});

let recallRuntime;
let restoreNodeOptions;
if (process.platform === "win32") {
  const shimPath = join(auditDir, "fake-repository-memory-shim.cjs").split("\\").join("/");
  await writeFile(shimPath, `process.stdout.write(${JSON.stringify(RECALL_PAYLOAD)});\nprocess.exit(0);\n`, { encoding: "utf8" });
  recallRuntime = process.execPath;
  const previousNodeOptions = process.env.NODE_OPTIONS;
  process.env.NODE_OPTIONS = `${previousNodeOptions ? `${previousNodeOptions} ` : ""}--require ${shimPath}`;
  restoreNodeOptions = () => {
    if (previousNodeOptions === undefined) delete process.env.NODE_OPTIONS;
    else process.env.NODE_OPTIONS = previousNodeOptions;
  };
} else {
  recallRuntime = join(auditDir, "fake-repository-memory");
  await writeFile(recallRuntime, `#!/bin/sh\nprintf '%s' '${RECALL_PAYLOAD}'\n`, { encoding: "utf8" });
  await chmod(recallRuntime, 0o700);
}

const recallHooks = new Map();
const recallApi = {
  pluginConfig: { enabled: true, agentIds: ["yaole"], runtime: recallRuntime, auditPath: join(auditDir, "recall.jsonl") },
  logger: { info() {}, warn() {} },
  on(name, handler) { recallHooks.set(name, handler); },
};
plugin.register(recallApi);
const recall = await recallHooks.get("before_prompt_build")({ prompt: "记住这个上下文" }, { agentId: "yaole", sessionKey: "recall-1" });
restoreNodeOptions?.();
assert.match(recall.prependContext, /accepted profile context/);
assert.doesNotMatch(recall.prependContext, /do not inject/);

// MemOS-style native OpenClaw tools and lifecycle coverage must be available
// without introducing a second retrieval implementation.  The fake runtime
// is the same executable used by the recall test, so this exercises the real
// spawn/JSON boundary rather than a mocked tool result.
const nativeTools = new Map();
const nativeHooks = new Map();
const nativeApi = {
  pluginConfig: { enabled: true, nativeTools: true, runtime: recallRuntime, auditPath: join(auditDir, "native-tools.jsonl") },
  logger: { info() {}, warn() {} },
  on(name, handler) { nativeHooks.set(name, handler); },
  registerTool(tool, options = {}) {
    const descriptor = typeof tool === "function" ? tool({ agentId: "yaole", sessionKey: "native-session" }) : tool;
    nativeTools.set(options.name || descriptor.name, descriptor);
  },
};
plugin.register(nativeApi);
for (const name of ["repository_memory_doctor", "repository_memory_search", "repository_memory_get", "repository_memory_timeline"]) {
  assert.equal(nativeTools.has(name), true, `native tool ${name} should be registered`);
}
for (const name of ["before_prompt_build", "before_agent_run", "agent_end", "session_start", "session_end", "tool_result_persist", "before_tool_call", "after_tool_call"]) {
  assert.equal(nativeHooks.has(name), true, `lifecycle hook ${name} should be registered`);
}
const nativeSearch = await nativeTools.get("repository_memory_search").execute("call-1", { query: "memory" });
assert.equal(nativeSearch.isError, undefined);
assert.match(nativeSearch.content[0].text, /accepted profile context/);
const nativeTimeline = await nativeTools.get("repository_memory_timeline").execute("call-2", { session_id: "native-session" });
assert.equal(nativeTimeline.isError, undefined);

console.log("openclaw guard ok");
