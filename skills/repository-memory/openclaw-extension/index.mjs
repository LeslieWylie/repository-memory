import { appendFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";

const PLUGIN_ID = "repository-memory-autocapture";
const DEFAULT_TIMEOUT_MS = 15000;
const STATE_TTL_MS = 30 * 60 * 1000;
const completionKeys = new Set();
const runStates = new Map();
const activeAgentStates = new Map();

function text(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => text(item)).filter(Boolean).join("\n");
  if (value && typeof value === "object") return text(value.text || value.content || "");
  return "";
}

function optional(value) {
  const result = text(value).trim();
  return result || undefined;
}

function digest(value) {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value || {})).digest("hex").slice(0, 24);
}

function messagesFrom(event, maxMessages, maxMessageChars) {
  const messages = Array.isArray(event?.messages) ? event.messages : [];
  return messages.slice(-maxMessages).flatMap((message) => {
    const role = optional(message?.role);
    if (role !== "user" && role !== "assistant") return [];
    const content = text(message?.content).trim().slice(0, maxMessageChars);
    return content ? [{ role, content }] : [];
  });
}

function sessionId(ctx, event) {
  return optional(ctx?.sessionKey) || optional(ctx?.sessionId) || optional(event?.sessionKey) || optional(event?.sessionId);
}

function runId(ctx, event, messages = []) {
  const explicit = optional(ctx?.runId) || optional(event?.runId) || optional(event?.turnId);
  if (explicit) return explicit;
  return digest(messages.map((item) => `${item.role}:${item.content}`).join("\n"));
}

function stateKey(ctx, event, messages = []) {
  // OpenClaw does not guarantee that every lifecycle hook carries the same
  // runId.  A session is serialized by the gateway, so use it as the stable
  // correlation key and keep the run id only for capture/audit idempotency.
  // This prevents a valid doctor/search/get sequence from disappearing when
  // the tool hook omits runId.
  return sessionId(ctx, event) || `agent:${optional(ctx?.agentId) || "main"}`;
}

function agentState(cfg, ctx, event) {
  const key = stateKey(ctx, event);
  const agent = optional(ctx?.agentId) || "main";
  const state = sessionId(ctx, event) ? runStates.get(key) : activeAgentStates.get(agent);
  if (state && Date.now() - state.startedAt > STATE_TTL_MS) {
    runStates.delete(key);
    if (activeAgentStates.get(agent) === state) activeAgentStates.delete(agent);
    return undefined;
  }
  if (state && !runStates.has(key)) runStates.set(key, state);
  return state;
}

function isAllowedAgent(cfg, ctx) {
  if (!Array.isArray(cfg.agentIds) || cfg.agentIds.length === 0) return true;
  const agentId = optional(ctx?.agentId) || "main";
  return cfg.agentIds.includes(agentId);
}

function isExplicitDirectOperation(prompt) {
  return /直接(?:读|看|打开|检查)文件|运行命令|执行命令|read\s+the\s+file|open\s+the\s+file|run\s+(?:a\s+)?command|cat\s+|sed\s+|grep\s+/i.test(prompt);
}

function isMaintenancePrompt(prompt) {
  if (!prompt) return false;
  if (isExplicitDirectOperation(prompt)) return true;
  return /修复|实现|重构|审计|审核|评审|检查代码|改代码|开发|调试|(?:测试)(?:代码|用例|失败|一下|这个)|(?:验证)(?:修复|结果是否|一下|这个)|生成|写入|写日报|更新日报|(?:更新)(?:代码|分支|上去|一下|这个)|创建|修改|删除|安装|升级|(?:提交)(?:代码|变更|这个分支|到|上去|PR)|推送|合并|rebase|重启|启动|停止|排查|诊断|运行|执行|同步|拉取|交付|交了|整理|发布|部署|fix|implement|refactor|audit|review|(?:test)\s+(?:code|suite|failure)|verify\s+(?:the\s+)?(?:fix|result)|generate|write|update\s+(?:code|branch|report)|create|modify|delete|install|upgrade|commit\s+(?:changes|this|the\s+branch)|push|merge|restart|start|stop|diagnose|run|sync|deploy/i.test(prompt);
}

function isRepositoryFactPrompt(prompt) {
  if (!prompt || isMaintenancePrompt(prompt)) return false;
  return /记忆|知识库|仓库|代码库|实验结果|评测结果|日报|周报|历史报告|研究结论|最近在做|最近做了什么|上次|之前|进展|状态|根据记录|来源|证据|citation|repository|repo\b|experiment|evaluation|benchmark|latest|recent|history|report|according to/i.test(prompt);
}

function promptPolicy(prompt) {
  if (isRepositoryFactPrompt(prompt)) return "repository-fact";
  if (isMaintenancePrompt(prompt)) return "maintenance";
  return "ordinary";
}

function repoTool(cfg, toolName, suffix) {
  const prefix = cfg.repoToolPrefix;
  return toolName === `${prefix}${suffix}` || (prefix === "" && toolName === suffix) || toolName === `repository-memory__${suffix}`;
}

function bareHostMemoryTool(toolName) {
  return toolName === "memory_search" || toolName === "memory_get";
}

function directTool(toolName) {
  const name = String(toolName || "").toLowerCase();
  if (["read", "exec", "code_mode_exec", "write", "edit", "apply_patch", "file_read", "file_write", "file_edit", "delete", "shell", "terminal", "git"].includes(name)) return true;
  if (/(?:^|[._-])(?:fs|filesystem|file)(?:$|[._-])/.test(name)) return true;
  return /(?:^|[._-])(exec|shell|terminal|bash|zsh|python|read|write|edit|patch|file|git)(?:$|[._-])/.test(name);
}

function directToolInput(event) {
  const params = event?.params && typeof event.params === "object" ? event.params : {};
  return text(params.command || params.cmd || params.script || params.code || params.input || "").trim();
}

function isSafeRecoveryCommand(command) {
  if (!command || /[;&|`$<>]/.test(command)) return false;
  if (/\b(?:rm|rmdir|mkfs|shutdown|reboot|git\s+(?:reset|clean|checkout\s+--|rebase|push)|kill\s+-9)\b/i.test(command)) return false;
  return /\b(?:repository-memory|rlvr-memory|memorycore)\b[\s\S]*\b(?:doctor|status|probe|health|restart|start|stop|reload|version|mcp)\b/i.test(command)
    || /\bopenclaw\s+(?:mcp\s+probe|doctor|plugins\s+inspect)\b/i.test(command)
    || /\blaunchctl\s+(?:print|list|kickstart)\b/i.test(command)
    || /\b(?:ps|lsof)\b/i.test(command);
}

function parseResult(value) {
  if (value && typeof value === "object") {
    if (Array.isArray(value)) {
      for (const item of value) {
        const parsed = parseResult(item);
        if (Object.keys(parsed).length) return parsed;
      }
      return {};
    }
    if (isResultPayload(value)) return value;
    if (value.structuredContent && typeof value.structuredContent === "object") {
      const parsedStructured = parseResult(value.structuredContent);
      if (isResultPayload(parsedStructured)) return parsedStructured;
    }
    if (value.details && typeof value.details === "object") {
      const parsedDetails = parseResult(value.details);
      if (isResultPayload(parsedDetails)) return parsedDetails;
    }
    if (value.result && typeof value.result === "object") {
      const parsedResult = parseResult(value.result);
      if (isResultPayload(parsedResult)) return parsedResult;
    }
    if (Array.isArray(value.content)) {
      for (const item of value.content) {
        const parsed = parseResult(item?.text || item?.content);
        if (isResultPayload(parsed)) return parsed;
      }
    }
    return value;
  }
  if (typeof value !== "string") return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function isResultPayload(value) {
  return Boolean(value && typeof value === "object" && (
    Array.isArray(value.verified) || Array.isArray(value.candidates) || value.groups ||
    Object.prototype.hasOwnProperty.call(value, "abstain") || Object.prototype.hasOwnProperty.call(value, "ok") || Object.prototype.hasOwnProperty.call(value, "error") || value.status || value.freshness || value.found
  ));
}

function resultCounts(value) {
  const result = parseResult(value);
  const verified = Array.isArray(result.verified) ? result.verified : Array.isArray(result.results) ? result.results : [];
  const groups = result.groups && typeof result.groups === "object" ? Object.values(result.groups) : [];
  const groupedVerified = groups.flatMap((group) => Array.isArray(group?.verified) ? group.verified : []);
  const items = groupedVerified.length ? groupedVerified : verified;
  const citations = items.filter((item) => item?.citation?.valid === true || item?.citation_valid === true).length;
  const freshnessValue = result.freshness;
  const freshness = freshnessValue && typeof freshnessValue === "object"
    ? (freshnessValue.state || [...new Set(Object.values(freshnessValue).map((item) => item?.state).filter(Boolean))].sort())
    : freshnessValue || null;
  const failed = result.ok === false || Boolean(result.error) || result.status === "error";
  return { verified: items.length, citations, abstain: result.abstain === true, freshness, failed };
}

function resultShape(value) {
  if (value === undefined || value === null) return "missing";
  if (typeof value === "string") return `string:${value.length}`;
  if (Array.isArray(value)) return `array:${value.length}:${value[0] && typeof value[0] === "object" ? Object.keys(value[0]).sort().join(",") : typeof value[0]}`;
  if (typeof value === "object") return `object:${Object.keys(value).sort().join(",")}`;
  return typeof value;
}

function finalAnswerText(event) {
  return text(event?.finalText || event?.answer || event?.response || event?.message || event?.content).trim();
}

function hasExplicitAbstention(answer) {
  return /abstain\s*[:=]\s*true|没有(?:找到|可验证|足够)|无法(?:确认|验证)|证据不足|不能确认|不作结论|拒绝回答/i.test(answer);
}

function hasEvidenceReceipt(answer) {
  const source = /citation|source|repository|来源|仓库|证据/i.test(answer);
  const commit = /(?:commit|提交)\s*[:=]?\s*(?:[0-9a-f]{7,40}|[a-z0-9._/-]+)/i.test(answer);
  const path = /(?:path|路径|文件)\s*[:=]?\s*[^\s,，;；]+/i.test(answer) || /(?:^|\s)[./~][^\s,，;；]+/.test(answer);
  const line = /(?:line|行号|行|#L)\s*[:=]?\s*\d+/i.test(answer);
  const fresh = /fresh(?:ness)?|新鲜|最新快照|remote_snapshot|远程快照/i.test(answer);
  return source && commit && path && line && fresh;
}

async function appendAudit(cfg, event) {
  const path = cfg.auditPath || join(homedir(), ".local", "share", "repository-memory", "openclaw-tool-audit.jsonl");
  try {
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    await appendFile(path, `${JSON.stringify({ timestamp: new Date().toISOString(), ...event })}\n`, { encoding: "utf8", mode: 0o600 });
  } catch {
    // Auditing must never turn a completed agent response into a crash.
  }
}

function runCapture(cfg, payload) {
  const runtime = optional(cfg.runtime) || "repository-memory";
  const timeoutMs = Number.isFinite(Number(cfg.timeoutMs)) ? Math.max(1000, Math.min(60000, Number(cfg.timeoutMs))) : DEFAULT_TIMEOUT_MS;
  const dirPromise = mkdtemp(join(tmpdir(), "repository-memory-turn-"));
  return dirPromise.then(async (dir) => {
    const input = join(dir, "turn.json");
    await writeFile(input, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 });
    return await new Promise((resolve) => {
      const child = spawn(runtime, ["capture-turn", "--input", input, "--json"], { stdio: ["ignore", "pipe", "pipe"], env: process.env });
      let stdout = "";
      let stderr = "";
      child.stdout?.on("data", (chunk) => { stdout = (stdout + String(chunk)).slice(-65536); });
      child.stderr?.on("data", (chunk) => { stderr = (stderr + String(chunk)).slice(-8192); });
      const timer = setTimeout(() => {
        child.kill("SIGTERM");
        resolve({ ok: false, error: "capture runtime timeout" });
      }, timeoutMs);
      child.on("error", (error) => {
        clearTimeout(timer);
        resolve({ ok: false, error: error.message });
      });
      child.on("close", (code) => {
        clearTimeout(timer);
        if (code !== 0) {
          resolve({ ok: false, error: (stderr || stdout || `runtime exited ${code}`).trim().slice(0, 400) });
          return;
        }
        try {
          resolve({ ok: true, result: JSON.parse(stdout) });
        } catch {
          resolve({ ok: false, error: "capture runtime returned invalid JSON" });
        }
      });
    }).finally(() => rm(dir, { recursive: true, force: true }).catch(() => {}));
  }).catch((error) => ({ ok: false, error: error.message }));
}

export default {
  id: PLUGIN_ID,
  name: "Repository Memory Auto Capture and Guard",
  description: "Enforce repository-memory evidence routing and capture completed turns into the shared L0-L2 pipeline.",
  register(api) {
    const raw = api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {};
    const cfg = {
      enabled: raw.enabled !== false,
      guardEnabled: raw.guardEnabled === true,
      runtime: raw.runtime,
      auditPath: raw.auditPath,
      repoToolPrefix: typeof raw.repoToolPrefix === "string" ? raw.repoToolPrefix : "repository-memory__",
      agentIds: Array.isArray(raw.agentIds) ? raw.agentIds.filter((item) => typeof item === "string") : [],
      maxMessages: Math.max(4, Math.min(64, Number(raw.maxMessages) || 24)),
      maxMessageChars: Math.max(1000, Math.min(50000, Number(raw.maxMessageChars) || 12000)),
      timeoutMs: Math.max(1000, Math.min(60000, Number(raw.timeoutMs) || DEFAULT_TIMEOUT_MS)),
    };
    if (!cfg.enabled) return;

    if (cfg.guardEnabled) {
      api.on("before_agent_run", async (event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return { outcome: "pass" };
        const prompt = optional(event?.prompt) || optional(event?.message) || "";
        const key = stateKey(ctx, event);
        const policy = promptPolicy(prompt);
        runStates.set(key, {
          mode: policy,
          strict: policy === "repository-fact",
          doctor: false,
          doctorRequested: false,
          doctorCompleted: false,
          doctorFailed: false,
          search: false,
          searchCompleted: false,
          searchFailed: false,
          get: false,
          sync: false,
          recovery: false,
          verified: 0,
          citations: 0,
          abstain: false,
          revisionRequested: false,
          startedAt: Date.now(),
        });
        activeAgentStates.set(optional(ctx?.agentId) || "main", runStates.get(key));
        if (runStates.size > 512) runStates.delete(runStates.keys().next().value);
        await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "agent_run", intent: policy, correlation: sessionId(ctx, event) ? "session" : "agent-fallback" });
        return { outcome: "pass" };
      }, { priority: 100, timeoutMs: 5000 });

      api.on("before_tool_call", async (event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const toolName = optional(event?.toolName) || "unknown";
        const key = stateKey(ctx, event);
        const state = agentState(cfg, ctx, event);
        if (bareHostMemoryTool(toolName) && state?.strict) {
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "tool_blocked", tool: toolName, reason: "bare host memory backend is not repository-memory" });
          return { block: true, blockReason: "Use the namespaced repository-memory MCP tools. A bare host memory_search is not a repository citation source." };
        }
        if (!state?.strict) return;
        if (repoTool(cfg, toolName, "memory_search")) {
          if (!state.doctorCompleted) return { block: true, blockReason: "Call repository-memory__memory_doctor and wait for a successful result before repository search." };
          state.search = true;
          return;
        }
        if (repoTool(cfg, toolName, "memory_doctor")) {
          state.doctorRequested = true;
          return;
        }
        if (repoTool(cfg, toolName, "memory_sync")) {
          state.sync = true;
          return;
        }
        if (repoTool(cfg, toolName, "memory_get")) {
          if (!state.searchCompleted) return { block: true, blockReason: "Call repository-memory__memory_search before memory_get." };
          state.get = true;
          return;
        }
        if (directTool(toolName)) {
          const command = directToolInput(event);
          if (state.searchFailed && isSafeRecoveryCommand(command)) {
            state.recovery = true;
            await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "recovery_allowed", tool: toolName, reason: "repository-memory search failed; safe diagnostic/recovery command allowed" });
            return;
          }
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "tool_blocked", tool: toolName, reason: "repository-fact request must not bypass MCP" });
          return { block: true, blockReason: "Repository-fact turns must use repository-memory MCP. Direct file/shell fallback is blocked; after an MCP failure only a safe repository-memory diagnostic/recovery command is allowed." };
        }
      }, { priority: 100, timeoutMs: 5000 });

      api.on("after_tool_call", async (event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const toolName = optional(event?.toolName) || "unknown";
        const key = stateKey(ctx, event);
        const state = agentState(cfg, ctx, event);
        const repoSearch = repoTool(cfg, toolName, "memory_search");
        const result = event?.result ?? event?.output ?? event?.resultText;
        const counts = repoSearch ? resultCounts(result) : { verified: 0, citations: 0, abstain: false, freshness: null, failed: false };
        if (repoSearch && event?.error) counts.failed = true;
        if (repoSearch && state) {
          state.searchCompleted = true;
          state.searchFailed = counts.failed;
          state.verified = counts.verified;
          state.citations = counts.citations;
          state.abstain = counts.abstain;
        }
        if (state && repoTool(cfg, toolName, "memory_doctor")) {
          const doctorCounts = resultCounts(result);
          state.doctorCompleted = !Boolean(event?.error) && !doctorCounts.failed;
          state.doctorFailed = !state.doctorCompleted;
          state.doctor = state.doctorCompleted;
        }
        if (state && repoTool(cfg, toolName, "memory_get")) state.get = true;
        await appendAudit(cfg, {
          agent: optional(ctx?.agentId) || "main",
          run_id: optional(ctx?.runId) || null,
          event: "tool_completed",
          tool: toolName,
          input_hash: digest(event?.params || {}),
          scope: repoSearch ? "repository" : null,
          outcome: event?.error ? "error" : "completed",
          verified: counts.verified,
          citations: counts.citations,
          abstain: counts.abstain,
          freshness: counts.freshness,
          failed: counts.failed,
          result_shape: resultShape(event?.result ?? event?.output ?? event?.resultText),
        });
      }, { priority: -100, timeoutMs: 5000 });

      api.on("before_agent_finalize", async (_event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const key = stateKey(ctx, _event);
        const state = agentState(cfg, ctx, _event);
        if (!state?.strict || state.revisionRequested) return;
        const answer = finalAnswerText(_event);
        const missingReceipt = Boolean(answer) && state.verified > 0 && !hasEvidenceReceipt(answer);
        const missingAbstain = Boolean(answer) && state.verified === 0 && !state.abstain && !hasExplicitAbstention(answer);
        if (!state.searchCompleted || (state.verified > 0 && !state.get) || missingReceipt || missingAbstain) {
          state.revisionRequested = true;
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "finalize_revision", reason: missingReceipt || missingAbstain ? "repository-memory answer receipt incomplete" : "repository-memory sequence incomplete", search_failed: state.searchFailed === true });
          return {
            action: "revise",
            reason: missingReceipt || missingAbstain ? "Repository-memory answer must include an evidence receipt or explicit abstention." : "Repository-memory evidence sequence was not observed.",
            retry: {
              instruction: "Use the namespaced repository-memory doctor/search/get tools. If search fails, you may run only a safe repository-memory diagnostic/recovery command; never read files or use an unrelated shell fallback. If no verified citation remains, answer with an explicit abstention.",
              idempotencyKey: `${key}:repository-memory-guard`,
              maxAttempts: 1,
            },
          };
        }
      }, { priority: 100, timeoutMs: 5000 });
    }

    api.on("agent_end", (event, ctx) => {
      if (!isAllowedAgent(cfg, ctx) || event?.success === false) return;
      const messages = messagesFrom(event, cfg.maxMessages, cfg.maxMessageChars);
      if (!messages.some((item) => item.role === "user") || !messages.some((item) => item.role === "assistant")) return;
      const turnId = runId(ctx, event, messages);
      const key = `${sessionId(ctx, event)}\u0000${turnId}`;
      if (completionKeys.has(key)) return;
      completionKeys.add(key);
      const state = agentState(cfg, ctx, event);
      void appendAudit(cfg, {
        agent: optional(ctx?.agentId) || "main",
        run_id: turnId,
        event: "agent_end",
        intent: state?.mode || "ordinary",
        doctor: state?.doctor === true,
        search: state?.searchCompleted === true,
        get: state?.get === true,
        recovery: state?.recovery === true,
        verified: Number(state?.verified || 0),
        citations: Number(state?.citations || 0),
        abstain: state?.abstain === true,
        latency_ms: state?.startedAt ? Math.max(0, Date.now() - state.startedAt) : null,
      });
      const payload = {
        session_id: sessionId(ctx, event),
        run_id: turnId,
        agent_id: optional(ctx?.agentId) || "main",
        workspace: optional(ctx?.workspaceDir),
        messages,
      };
      void runCapture(cfg, payload).then((outcome) => {
        if (!outcome.ok) {
          api.logger?.warn?.(`${PLUGIN_ID}: capture failed: ${outcome.error}`);
          return;
        }
        const result = outcome.result || {};
        api.logger?.info?.(`${PLUGIN_ID}: L0=${result.l0?.status || "unknown"}, L1=${result.l1?.status || "unknown"}, L2=${result.l2?.status || "skipped"}, L3=${result.l3?.status || "explicit-only"}`);
      });
      if (activeAgentStates.get(optional(ctx?.agentId) || "main") === state) {
        activeAgentStates.delete(optional(ctx?.agentId) || "main");
      }
    });
  },
};
