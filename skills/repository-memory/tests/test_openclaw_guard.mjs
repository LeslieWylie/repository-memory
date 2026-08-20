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
const missingReceipt = await hooks.get("before_agent_finalize")({ lastAssistantMessage: "评测结果是 90%" }, receiptCtx);
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
const weakReceipt = await hooks.get("before_agent_finalize")({ lastAssistantMessage: "来源是评测报告，结果是 90%" }, weakReceiptCtx);
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
await hooks.get("before_agent_finalize")({ lastAssistantMessage: "评测结果是 90%，可以直接下结论" }, partialCtx);

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
const auditFinalize = await auditHooks.get("before_agent_finalize")({ lastAssistantMessage: "结果待核实" }, auditCtx);
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
if (process.platform === "win32") {
  const shimPath = join(auditDir, "fake-repository-memory-shim.cjs").split("\\").join("/");
  await writeFile(shimPath, `process.stdout.write(${JSON.stringify(RECALL_PAYLOAD)});\nprocess.exit(0);\n`, { encoding: "utf8" });
  recallRuntime = process.execPath;
  const previousNodeOptions = process.env.NODE_OPTIONS;
  process.env.NODE_OPTIONS = `${previousNodeOptions ? `${previousNodeOptions} ` : ""}--require ${shimPath}`;
  // Restored at exit rather than after the recall test, because the preload has
  // to stay in effect for *every* spawn of recallRuntime -- and on Windows
  // recallRuntime is node.exe itself. Restoring inline reads as tidy and is a
  // Windows-only footgun: the recall spawn above it kept working while the
  // native-tools spawns below it received the fixed ["search", ...] argv with no
  // preload, tried to resolve "search" as a script, and came back isError:true.
  // Neither POSIX CI leg can reproduce that (there recallRuntime is the shell
  // script, which does not read NODE_OPTIONS), so it sat on the default branch
  // failing all three windows-latest legs. An exit handler makes the ordering
  // hazard structurally impossible instead of merely currently-correct.
  process.on("exit", () => {
    if (previousNodeOptions === undefined) delete process.env.NODE_OPTIONS;
    else process.env.NODE_OPTIONS = previousNodeOptions;
  });
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
for (const name of ["repository_memory_doctor", "repository_memory_search", "repository_memory_get", "repository_memory_timeline", "repository_memory_observe", "repository_memory_reflect"]) {
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
const nativeObserve = await nativeTools.get("repository_memory_observe").execute("call-3", { session_id: "native-session" });
assert.equal(nativeObserve.isError, undefined);
const nativeReflect = await nativeTools.get("repository_memory_reflect").execute("call-4", { query: "memory" });
assert.equal(nativeReflect.isError, undefined);

// This extension prepends its own recall block to the prompt, so intent has to
// be read from the user's words alone.  Recalled memory quotes maintenance
// vocabulary all the time; when the combined text was classified, a recalled
// line about an upgrade turned a plain question into a "maintenance" turn,
// which cleared `strict` and disabled every evidence guard for that run.
const recallCtx = { agentId: "yaole", sessionKey: "recall-intent-1", runId: "recall-intent-run" };
const recallPrompt = [
  "<repository-memory-context>",
  "The following is conversation memory, not repository citation.",
  "",
  "- [L1/accepted] octo-daemon 升级到 0.5.0，已重启并部署完成 (memory_id=local:L1:abc)",
  "",
  "</repository-memory-context>",
  "",
  "刘伯潇最近在做什么工作?",
].join("\n");
await hooks.get("before_agent_run")({ prompt: recallPrompt }, recallCtx);
// A repository-fact turn is strict, so a bare host memory tool is still
// observed rather than silently treated as an ordinary turn.
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "刘伯潇" } }, recallCtx);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: { verified: [{ id: "a" }], answerable: [], citations: [{ path: "standup/刘伯潇.md" }] },
}, recallCtx);
await hooks.get("before_agent_finalize")({ lastAssistantMessage: "刘伯潇在做短剧后期。" }, recallCtx);
const guardAudit = await readFile(join(auditDir, "audit.jsonl"), "utf8");
const finalizeWarnings = guardAudit.split("\n").filter(Boolean).map((line) => JSON.parse(line))
  .filter((row) => row.event === "finalize_warning" && row.run_id === "recall-intent-run");
assert.ok(
  finalizeWarnings.length > 0,
  "a recall-prefixed fact question must stay strict so the unsupported-claim guard can fire",
);

// `recallMaxChars` truncates the recall block.  Slicing the joined string would
// cut its own closing tag off, and the unbalanced block then survived the
// tag-pair strip above — putting recalled maintenance vocabulary straight back
// into intent classification.  The tag must always close, and the strip must
// hold even when the block is malformed.
const truncRuntime = join(auditDir, "fake-repository-memory-long");
const LONG_PAYLOAD = JSON.stringify({
  groups: {
    memory: {
      answerable: Array.from({ length: 40 }, (_, index) => ({
        id: `memorycore:L1:long-${index}`,
        memory_layer: "L1",
        status: "accepted",
        content: `octo-daemon 升级到 0.5.0，已重启并部署完成，这是第 ${index} 条足够长的召回内容用来撑破预算`,
        citation: { valid: true },
      })),
    },
  },
});
if (process.platform === "win32") {
  await writeFile(join(auditDir, "long-shim.cjs"), `process.stdout.write(${JSON.stringify(LONG_PAYLOAD)});\nprocess.exit(0);\n`, { encoding: "utf8" });
} else {
  await writeFile(truncRuntime, `#!/bin/sh\nprintf '%s' '${LONG_PAYLOAD}'\n`, { encoding: "utf8" });
  await chmod(truncRuntime, 0o700);
}
if (process.platform !== "win32") {
  const truncHooks = new Map();
  plugin.register({
    pluginConfig: { enabled: true, agentIds: ["yaole"], runtime: truncRuntime, recallMaxChars: 1000, auditPath: join(auditDir, "trunc.jsonl") },
    logger: { info() {}, warn() {} },
    on(name, handler) { truncHooks.set(name, handler); },
  });
  const truncated = await truncHooks.get("before_prompt_build")({ prompt: "记住这个上下文" }, { agentId: "yaole", sessionKey: "trunc-1" });
  const block = truncated.prependContext;
  assert.ok(block.length > 1000 * 0.5, "the fixture must be long enough to hit the truncation path");
  assert.match(block, /<\/repository-memory-context>$/, "a truncated recall block must still close its tag");
  assert.equal(
    `${block}\n\n刘伯潇最近在做什么工作?`.replace(/<repository-memory-context>[\s\S]*?<\/repository-memory-context>/g, " ").trim(),
    "刘伯潇最近在做什么工作?",
    "the tag-pair strip must remove a truncated block whole",
  );
}

// Both of these are plain historical fact recall, and both were misread by the
// lexical classifier in a live run: the first matched no fact keyword and came
// out `ordinary`, the second is worded entirely in maintenance vocabulary
// ("升级", "验证") and came out `maintenance`.  Under the old wiring either
// label cleared `strict` and every evidence guard went quiet for the turn.
// Following MemOS, the guards now read what retrieval returned, so the label is
// recorded but decides nothing.
for (const [index, prompt] of [
  "小队 agent 集体拒任务那次，根因是什么？",
  "octo-daemon 升级到哪个版本了？当时是怎么验证的？",
].entries()) {
  const misreadCtx = { agentId: "yaole", sessionKey: `misread-${index}`, runId: `misread-run-${index}` };
  await hooks.get("before_agent_run")({ prompt }, misreadCtx);
  await hooks.get("before_tool_call")({ toolName: "repository_memory_search", params: { query: prompt } }, misreadCtx);
  await hooks.get("after_tool_call")({
    toolName: "repository_memory_search",
    result: { verified: [{ id: "a" }], answerable: [], citations: [{ path: "notes.md" }] },
  }, misreadCtx);
  // A complete receipt isolates the unsupported-claim guard: the citation is
  // real and fully reported, it just does not support the claim being made.
  await hooks.get("before_agent_finalize")({ lastAssistantMessage: "根因是配额耗尽。来源：commit abc1234，路径 notes.md，行 12，freshness fresh。" }, misreadCtx);

  const rows = (await readFile(join(auditDir, "audit.jsonl"), "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const warned = rows.filter((row) => row.event === "finalize_warning" && row.run_id === `misread-run-${index}`);
  assert.equal(warned.length, 1, `the unsupported-claim guard must fire regardless of how "${prompt}" is labelled`);
  assert.match(warned[0].reason, /did not support the complete claim/);
  // The label is still written, and it is still wrong — that is the point of
  // keeping it: the audit can show the disagreement without acting on it.
  assert.notEqual(warned[0].intent, "repository-fact");
  const searched = rows.filter((row) => row.event === "tool_completed" && row.tool === "repository_memory_search");
  assert.ok(searched.length > 0, "tool tracking must not be gated on the classification either");
}

if (process.platform !== "win32") {
  // `missingRetrieval` is the one guard that fires when nothing was retrieved,
  // so it needs an independent signal that the turn wanted memory.  MemOS
  // computes `trigger_retrieval` from whether the memory it holds addresses the
  // question; the pre-turn recall has already run that search, so the signal is
  // its answerable count — measured, not inferred from wording.
  const recallRuntime = join(auditDir, "fake-repository-memory-recall");
  const RECALL_PAYLOAD = JSON.stringify({
    groups: { memory: { answerable: [{ id: "memorycore:L1:x", memory_layer: "L1", status: "accepted", content: "小队 agent 拒任务的根因是配额耗尽", citation: { valid: true } }] } },
  });
  await writeFile(recallRuntime, `#!/bin/sh\nprintf '%s' '${RECALL_PAYLOAD}'\n`, { encoding: "utf8" });
  await chmod(recallRuntime, 0o700);

  const signalHooks = new Map();
  const signalAudit = join(auditDir, "signal.jsonl");
  plugin.register({
    pluginConfig: { enabled: true, guardEnabled: true, agentIds: ["yaole"], runtime: recallRuntime, auditPath: signalAudit },
    logger: { info() {}, warn() {} },
    on(name, handler) { signalHooks.set(name, handler); },
  });

  // A maintenance-sounding prompt must still be recalled — the keyword gate on
  // recall was the same table one layer down.
  const signalCtx = { agentId: "yaole", sessionKey: "recall-signal-1", runId: "recall-signal-run" };
  const injected = await signalHooks.get("before_prompt_build")({ prompt: "octo-daemon 升级到哪个版本了？" }, signalCtx);
  assert.ok(injected?.prependContext, "recall must run on a maintenance-sounding question");
  await signalHooks.get("before_agent_run")({ prompt: "octo-daemon 升级到哪个版本了？" }, signalCtx);
  await signalHooks.get("before_agent_finalize")({ lastAssistantMessage: "升级到了 0.5.0。" }, signalCtx);

  const signalRows = (await readFile(signalAudit, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const skipped = signalRows.filter((row) => row.event === "finalize_warning");
  assert.equal(skipped.length, 1, "answering without retrieval must warn when recall found answerable memory");
  assert.match(skipped[0].reason, /skipped shared-memory retrieval/);
  assert.equal(skipped[0].recall_answerable, 1);

  // And when recall found nothing, the same shape of turn stays silent: the
  // guard follows the evidence, so an ordinary chat turn is not warned about.
  const quietRuntime = join(auditDir, "fake-repository-memory-quiet");
  await writeFile(quietRuntime, `#!/bin/sh\nprintf '%s' '${JSON.stringify({ groups: { memory: { answerable: [] } } })}'\n`, { encoding: "utf8" });
  await chmod(quietRuntime, 0o700);
  const quietHooks = new Map();
  const quietAudit = join(auditDir, "quiet.jsonl");
  plugin.register({
    pluginConfig: { enabled: true, guardEnabled: true, agentIds: ["yaole"], runtime: quietRuntime, auditPath: quietAudit },
    logger: { info() {}, warn() {} },
    on(name, handler) { quietHooks.set(name, handler); },
  });
  const quietCtx = { agentId: "yaole", sessionKey: "recall-quiet-1", runId: "recall-quiet-run" };
  await quietHooks.get("before_prompt_build")({ prompt: "帮我把这段话改得更短" }, quietCtx);
  await quietHooks.get("before_agent_run")({ prompt: "帮我把这段话改得更短" }, quietCtx);
  await quietHooks.get("before_agent_finalize")({ lastAssistantMessage: "改好了。" }, quietCtx);
  const quietRows = (await readFile(quietAudit, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  assert.equal(quietRows.filter((row) => row.event === "finalize_warning").length, 0, "an empty recall must not manufacture a retrieval warning");
}

// The finalize event's text fields are fixed by the host contract
// (`PluginHookBeforeAgentFinalizeEvent`: `lastAssistantMessage` and `messages`).
// Reading `finalText` instead meant the answer text was empty on every real
// turn, so all four guards evaluated to false and the audit went 1600+ rows
// without a single `finalize_warning` — while this suite passed, because its
// own fixtures used the field the extension expected rather than the field the
// host sends.  Drive both host-supplied shapes here.
for (const [index, event] of [
  { lastAssistantMessage: "评测结果是 90%" },
  { messages: [{ role: "user", content: "查询评测结果" }, { role: "assistant", content: "评测结果是 90%" }] },
].entries()) {
  const shapeCtx = { agentId: "yaole", sessionKey: `host-shape-${index}`, runId: `host-shape-run-${index}` };
  await hooks.get("before_agent_run")({ prompt: "查询评测结果" }, shapeCtx);
  await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "eval" } }, shapeCtx);
  await hooks.get("after_tool_call")({
    toolName: "repository-memory__memory_search",
    result: { verified: [{ citation: { valid: true } }], answerable: [{ id: "a" }], candidates: [], abstain: false, freshness: { state: "fresh" } },
  }, shapeCtx);
  await hooks.get("before_agent_finalize")(event, shapeCtx);
  const rows = (await readFile(join(auditDir, "audit.jsonl"), "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const warned = rows.filter((row) => row.event === "finalize_warning" && row.run_id === `host-shape-run-${index}`);
  assert.equal(warned.length, 1, "the receipt guard must read the answer text the host actually sends");
  assert.match(warned[0].reason, /receipt incomplete/);
}

// `scope=auto` keeps the top-level answer surface repository-only, so a turn
// answered from conversation memory or an accepted team decision arrives as
// `verified: 11, answerable: 0` — counted from different planes, and exactly
// the shape the unsupported-claim guard treats as a violation.  A live run hit
// this: a correct, fully cited memory-plane answer would have been warned about.
// When the payload is grouped, both counts come from the same groups.
const crossPlaneCtx = { agentId: "yaole", sessionKey: "cross-plane-1", runId: "cross-plane-run" };
await hooks.get("before_agent_run")({ prompt: "octo-daemon 升级到哪个版本了？" }, crossPlaneCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "octo-daemon" } }, crossPlaneCtx);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: {
    verified: [], answerable: [], abstain: true, answered_by: ["memory", "team"],
    groups: {
      repository: { verified: [{ citation: { valid: true } }], answerable: [] },
      memory: { verified: [{ id: "m1" }], answerable: [{ id: "m1" }] },
      team: { active: [{ id: "t1" }], candidates: [{ id: "t2" }] },
    },
  },
}, crossPlaneCtx);
await hooks.get("before_agent_finalize")({
  lastAssistantMessage: "升级到了 0.5.0。来源：commit fcec9177，路径 standup/x.md，行 296，远程快照。",
}, crossPlaneCtx);
const crossPlaneRows = (await readFile(join(auditDir, "audit.jsonl"), "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
const crossPlaneCompleted = crossPlaneRows.filter((row) => row.event === "tool_completed" && row.tool === "repository-memory__memory_search").at(-1);
assert.equal(crossPlaneCompleted.verified, 2, "verified counts every plane's verified items");
assert.equal(crossPlaneCompleted.answerable, 2, "answerable must come from the same planes: memory answerable plus active team records");
assert.equal(crossPlaneCompleted.abstain, false, "a memory-plane answer must not be recorded as an abstention");
assert.equal(
  crossPlaneRows.filter((row) => row.event === "finalize_warning" && row.run_id === "cross-plane-run").length,
  0,
  "a correct cross-plane answer with a full receipt must not be warned about",
);
// Candidates alone are still not an answer.
const candidateOnlyCtx = { agentId: "yaole", sessionKey: "candidate-only-1", runId: "candidate-only-run" };
await hooks.get("before_agent_run")({ prompt: "octo-daemon 升级到哪个版本了？" }, candidateOnlyCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "octo-daemon" } }, candidateOnlyCtx);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: {
    verified: [], answerable: [],
    groups: {
      repository: { verified: [{ citation: { valid: true } }], answerable: [] },
      team: { active: [], candidates: [{ id: "t2" }] },
    },
  },
}, candidateOnlyCtx);
await hooks.get("before_agent_finalize")({
  lastAssistantMessage: "升级到了 0.5.0。来源：commit fcec9177，路径 standup/x.md，行 296，远程快照。",
}, candidateOnlyCtx);
const candidateRows = (await readFile(join(auditDir, "audit.jsonl"), "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
assert.equal(candidateRows.filter((row) => row.event === "tool_completed" && row.tool === "repository-memory__memory_search").at(-1).answerable, 0);
const candidateWarned = candidateRows.filter((row) => row.event === "finalize_warning" && row.run_id === "candidate-only-run");
assert.equal(candidateWarned.length, 1, "candidates are leads, not support: claiming from them must still warn");
assert.match(candidateWarned[0].reason, /did not support the complete claim/);

// The receipt check must test the answer against the evidence that was
// actually returned, not against a list of approved words.  The first real
// answer this guard ever evaluated in production was flagged for a missing
// receipt while ending "依据：`rlvr-auto-survey/standup/武垚乐.md`，commit
// `e7a3dad5...`，约第 296-307 行" — a complete, checkable citation.  "依据" was
// not in the source vocabulary and no freshness word appeared.
const evidenceReceiptCtx = { agentId: "yaole", sessionKey: "receipt-1", runId: "receipt-run" };
await hooks.get("before_agent_run")({ prompt: "octo-daemon 升级到哪个版本了？当时是怎么验证的？" }, evidenceReceiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "octo-daemon" } }, evidenceReceiptCtx);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: {
    verified: [], answerable: [], answered_by: ["repository"],
    groups: {
      repository: {
        verified: [{ citation: { valid: true, commit: "e7a3dad5cd942afcb1d2cfda61f47c29c18da7b1", path: "rlvr-auto-survey/standup/武垚乐.md" } }],
        answerable: [{ citation: { valid: true, commit: "e7a3dad5cd942afcb1d2cfda61f47c29c18da7b1", path: "rlvr-auto-survey/standup/武垚乐.md" } }],
      },
    },
  },
}, evidenceReceiptCtx);
await hooks.get("before_agent_finalize")({
  lastAssistantMessage: "升级到 0.5.0。\n依据：`rlvr-auto-survey/standup/武垚乐.md`，commit `e7a3dad5cd942afcb1d2cfda61f47c29c18da7b1`，约第 296–307 行。",
}, evidenceReceiptCtx);
const evidenceReceiptRows = (await readFile(join(auditDir, "audit.jsonl"), "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
assert.equal(
  evidenceReceiptRows.filter((row) => row.event === "finalize_warning" && row.run_id === "receipt-run").length,
  0,
  "an answer naming the returned commit and path has shown its work, whatever word introduces it",
);

// An answer that cites nothing the tool returned is still flagged.
const noReceiptCtx = { agentId: "yaole", sessionKey: "no-receipt-1", runId: "no-receipt-run" };
await hooks.get("before_agent_run")({ prompt: "octo-daemon 升级到哪个版本了？" }, noReceiptCtx);
await hooks.get("before_tool_call")({ toolName: "repository-memory__memory_search", params: { query: "octo-daemon" } }, noReceiptCtx);
await hooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  result: {
    verified: [], answerable: [], answered_by: ["repository"],
    groups: {
      repository: {
        verified: [{ citation: { valid: true, commit: "e7a3dad5cd942afcb1d2cfda61f47c29c18da7b1", path: "rlvr-auto-survey/standup/武垚乐.md" } }],
        answerable: [{ citation: { valid: true, commit: "e7a3dad5cd942afcb1d2cfda61f47c29c18da7b1", path: "rlvr-auto-survey/standup/武垚乐.md" } }],
      },
    },
  },
}, noReceiptCtx);
await hooks.get("before_agent_finalize")({ lastAssistantMessage: "升级到 0.5.0。" }, noReceiptCtx);
const noReceiptRows = (await readFile(join(auditDir, "audit.jsonl"), "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
const noReceiptWarned = noReceiptRows.filter((row) => row.event === "finalize_warning" && row.run_id === "no-receipt-run");
assert.equal(noReceiptWarned.length, 1, "an answer that names none of the returned evidence must still be flagged");
assert.match(noReceiptWarned[0].reason, /receipt/);

// The recall hook runs `--scope memory`, and that scope answers on the
// top-level fields with `groups` left null; only `auto`/`all` populate
// `groups.memory`.  Every fixture above uses the `groups` shape, which is
// exactly why this went unnoticed: in the live replay the audit recorded
// `outcome: "injected"` alongside `answerable: 0`, and the finalize guard was
// told recall had found nothing.  Pin the shape the hook actually receives.
const planeRuntime = join(auditDir, "fake-repository-memory-plane");
const PLANE_PAYLOAD = JSON.stringify({
  groups: null,
  verified: [{ id: "local:L1:a", status: "verified" }],
  answerable: [
    { id: "local:L1:a", memory_layer: "L1", status: "verified", content: "octo-daemon 升级到 0.5.0，commit fcec9177", citation: { valid: true } },
    { id: "local:L1:b", memory_layer: "L1", status: "verified", content: "验证方式是跑真实 issue/autopilot", citation: { valid: true } },
  ],
});
await writeFile(planeRuntime, `#!/bin/sh\nprintf '%s' '${PLANE_PAYLOAD}'\n`, { encoding: "utf8" });
await chmod(planeRuntime, 0o700);

const planeHooks = new Map();
const planeAudit = join(auditDir, "plane.jsonl");
plugin.register({
  pluginConfig: { enabled: true, guardEnabled: true, agentIds: ["yaole"], runtime: planeRuntime, auditPath: planeAudit },
  logger: { info() {}, warn() {} },
  on(name, handler) { planeHooks.set(name, handler); },
});
const planeCtx = { agentId: "yaole", sessionKey: "recall-plane-1", runId: "recall-plane-run" };
const planeInjected = await planeHooks.get("before_prompt_build")({ prompt: "octo-daemon 升级到哪个版本了？" }, planeCtx);
assert.ok(planeInjected?.prependContext, "a top-level-shaped recall payload must still inject");
const planeRows = (await readFile(planeAudit, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
const planeRecall = planeRows.find((row) => row.event === "memory_recall");
assert.equal(planeRecall.outcome, "injected");
assert.equal(planeRecall.answerable, 2, "the audit must count what was injected, not read a key this scope leaves empty");
assert.equal(planeRecall.verified, 1);

// An answer drawn from conversation memory has no Git receipt to give, and
// demanding one made the live guard flag a correct answer: the turn searched
// with `scope: auto`, the runtime reported `answered_by: ["memory"]`, and the
// guard read the all-plane `verified: 20` and asked for a commit and a path
// that do not exist.  Scope the demand to the plane that can produce one — and
// to what that plane found *answerable*.  This fixture is the live shape: the
// repository plane returned five standup files that merely mention the daemon
// and then disowned them itself with `answerable: []` / `abstain: true`.
// Counting its `verified` instead kept the false warning alive through a
// second replay.
const memoryOnlyRuntime = join(auditDir, "fake-repository-memory-memory-only");
const memoryOnlyHooks = new Map();
const memoryOnlyAudit = join(auditDir, "memory-only.jsonl");
plugin.register({
  pluginConfig: { enabled: true, guardEnabled: true, agentIds: ["yaole"], runtime: memoryOnlyRuntime, auditPath: memoryOnlyAudit },
  logger: { info() {}, warn() {} },
  on(name, handler) { memoryOnlyHooks.set(name, handler); },
});
const memoryOnlyCtx = { agentId: "yaole", sessionKey: "memory-only-1", runId: "memory-only-run" };
await memoryOnlyHooks.get("before_agent_run")({ prompt: "octo-daemon 当时怎么验证的？" }, memoryOnlyCtx);
await memoryOnlyHooks.get("after_tool_call")({
  toolName: "repository-memory__memory_search",
  runId: "memory-only-run",
  result: {
    answered_by: ["memory"],
    groups: {
      repository: {
        verified: [
          { id: "rlvr-auto-survey:standup/武垚乐.md", citation: { valid: true, commit: "e7a3dad5cd942afcb1d2cfda61f47c29c18da7b1", path: "rlvr-auto-survey/standup/武垚乐.md" } },
          { id: "rlvr-auto-survey:standup/李宁.md", citation: { valid: true, commit: "e7a3dad5cd942afcb1d2cfda61f47c29c18da7b1", path: "rlvr-auto-survey/standup/李宁.md" } },
        ],
        answerable: [],
        abstain: true,
      },
      memory: {
        verified: [{ id: "local:L1:a", status: "verified" }],
        answerable: [{ id: "local:L1:a", status: "verified", content: "验证方式是跑真实 issue/autopilot" }],
      },
      team: { active: [], candidates: [{ id: "team:c1" }], abstain: true },
    },
  },
}, memoryOnlyCtx);
await memoryOnlyHooks.get("before_agent_finalize")({ lastAssistantMessage: "当时是跑真实 issue/autopilot 验证的。" }, memoryOnlyCtx);
const memoryOnlyRows = (await readFile(memoryOnlyAudit, "utf8")).split("\n").filter(Boolean).map((line) => JSON.parse(line));
assert.equal(
  memoryOnlyRows.filter((row) => row.event === "finalize_warning" && /receipt/.test(row.reason || "")).length,
  0,
  "a memory-plane answer must not be asked for a Git receipt",
);

console.log("openclaw guard ok");
