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
const captureBoundaryStates = new Map();

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

function numeric(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return undefined;
}

function captureBoundary(event, prompt) {
  const originalUserMessageCount = [
    event?.originalUserMessageCount,
    event?.original_user_message_count,
    event?.messageCountBeforeTurn,
    event?.message_count_before_turn,
  ].map(numeric).find((value) => value !== undefined && value >= 0);
  const afterTimestamp = event?.afterTimestamp ?? event?.after_timestamp ?? event?.captureCursor?.afterTimestamp ?? event?.capture_cursor?.after_timestamp;
  return {
    originalUserText: optional(event?.originalUserText) || optional(event?.original_user_text) || optional(prompt),
    originalUserMessageCount,
    afterTimestamp,
  };
}

function messagesFrom(event, maxMessages, maxMessageChars, boundary) {
  const allMessages = Array.isArray(event?.messages) ? event.messages : [];
  const start = numeric(boundary?.originalUserMessageCount);
  const messages = start !== undefined && start >= 0 && start <= allMessages.length
    ? allMessages.slice(start)
    : allMessages.slice(-maxMessages);
  return messages.slice(-maxMessages).flatMap((message) => {
    const role = optional(message?.role);
    if (role !== "user" && role !== "assistant") return [];
    const content = text(message?.content).trim().slice(0, maxMessageChars);
    if (!content) return [];
    const item = { role, content };
    if (message?.timestamp !== undefined) item.timestamp = message.timestamp;
    return [item];
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
  return /修复|实现|重构|审计|审核|评审|检查代码|改代码|开发|调试|(?:测试)(?:代码|用例|失败|一下|这个)|(?:验证)(?:修复|结果是否|一下|这个)|生成|写入|写日报|更新日报|(?:更新)(?:代码|分支|上去|一下|这个|报告)|创建|修改|删除|安装|升级|(?:提交)(?:代码|变更|这个分支|到|上去|PR)|推送|合并|rebase|重启|启动|停止|排查|诊断|交付|交了|整理|发布|部署|fix|implement|refactor|audit|review|(?:test)\s+(?:code|suite|failure)|verify\s+(?:the\s+)?(?:fix|result)|generate|write|update\s+(?:code|branch|report)|create|modify|delete|install|upgrade|commit\s+(?:changes|this|the\s+branch)|push|merge|restart|start|stop|diagnose|deploy/i.test(prompt)
    || /(?:运行|执行)\s*(?:命令|测试|脚本|任务|用例|pytest|npm|一下)|(?:同步|拉取)\s*(?:仓库|分支|源|代码|文件|一下)|\b(?:run|sync)\s+(?:tests?|commands?|scripts?|repo(?:sitory)?|branch)/i.test(prompt);
}

function isRepositoryFactPrompt(prompt) {
  if (!prompt || isMaintenancePrompt(prompt)) return false;
  return /记忆|知识库|仓库|代码库|实验结果|评测结果|日报|周报|历史报告|研究结论|最近|最新|运行结果|最近在做|最近做了什么|上次|之前|进展|状态|根据记录|来源|证据|citation|repository|repo\b|experiment|evaluation|benchmark|latest|recent|history|report|according to/i.test(prompt);
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

function directToolKind(toolName) {
  const name = String(toolName || "").toLowerCase().trim();
  const fileReadNames = new Set([
    "read",
    "file_read",
    "read_file",
    "filesystem.readfile",
    "filesystem.read_file",
    "fs.readfile",
    "fs.read_file",
  ]);
  if (fileReadNames.has(name)) return "file-read";
  if (/(?:^|[._:-])(?:filesystem|fs|file)(?:[._:-])(?:read|readfile|read_file)$/.test(name)) return "file-read";

  // Only recognize an actual shell/code execution surface.  Do not classify
  // arbitrary tools such as ``file_search`` or ``gitlab_search`` as direct
  // access merely because their names contain a matching substring.
  if (["exec", "shell", "terminal", "bash", "zsh", "python", "python3", "code_mode_exec", "code.exec", "git"].includes(name)) return "shell";
  if (/(?:^|[._:-])(?:exec|shell|terminal|bash|zsh)$/.test(name)) return "shell";
  return null;
}

function directToolInput(event) {
  const params = event?.params && typeof event.params === "object" ? event.params : {};
  const toolName = String(event?.toolName || "").toLowerCase();
  const value = params.command || params.cmd || params.script || params.code || params.input || params.path || params.file || params.args || "";
  return `${toolName === "git" && params.args ? "git " : ""}${text(value)}`.trim();
}

function isEvidenceReadCommand(command) {
  if (!command) return false;
  // These are high-confidence source-reading commands.  Generic exec such as
  // pytest, npm, make, git status, or a build command remains available even
  // during a repository-fact turn.
  if (/(?:^|[;&|]\s*)(?:cat|sed|grep|rg|awk|head|tail|less|more|bat)\b/i.test(command)) return true;
  if (/(?:^|[;&|]\s*)git\s+(?:show|log|grep)\b/i.test(command)) return true;
  return /\b(?:open|read_text|read_bytes|readFile|readFileSync)\s*\(/i.test(command);
}

function isDestructiveCommand(command) {
  return /\b(?:rm|rmdir|mkfs|shutdown|reboot|kill\s+-9|git\s+(?:reset|clean|checkout\s+--|rebase|push))\b/i.test(command);
}

function isSafeRecoveryCommand(command) {
  if (!command || /[;&|`$<>]/.test(command)) return false;
  if (/\b(?:rm|rmdir|mkfs|shutdown|reboot|git\s+(?:reset|clean|checkout\s+--|rebase|push)|kill\s+-9)\b/i.test(command)) return false;
  return /\b(?:repository-memory|memorycore)\b[\s\S]*\b(?:doctor|status|probe|health|restart|start|stop|reload|version|mcp)\b/i.test(command)
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
  const contextEvidence = Array.isArray(result.context?.repository_evidence) ? result.context.repository_evidence : [];
  const groups = result.groups && typeof result.groups === "object" ? Object.values(result.groups) : [];
  const groupedVerified = groups.flatMap((group) => Array.isArray(group?.verified) ? group.verified : []);
  const groupedAnswerable = groups.flatMap((group) => Array.isArray(group?.answerable) ? group.answerable : []);
  const items = groupedVerified.length ? groupedVerified : contextEvidence.length ? contextEvidence : verified;
  // New runtimes expose ``answerable`` separately from document-level
  // ``verified``.  Keep the fallback for older MCP payloads so this extension
  // remains compatible during a rolling install, but never fall back when the
  // field is explicitly present and empty.
  const answerable = Array.isArray(result.answerable)
    ? result.answerable
    : groupedAnswerable.length
      ? groupedAnswerable
      : items;
  const citations = items.filter((item) => item?.citation?.valid === true || item?.citation_valid === true).length;
  const freshnessValue = result.freshness;
  const freshness = freshnessValue && typeof freshnessValue === "object"
    ? (freshnessValue.state || [...new Set(Object.values(freshnessValue).map((item) => item?.state).filter(Boolean))].sort())
    : freshnessValue || null;
  const failed = result.ok === false || Boolean(result.error) || result.status === "error";
  const claimAbstain = result.claim_abstain === true || (items.length > 0 && answerable.length === 0);
  return {
    verified: items.length,
    answerable: answerable.length,
    citations,
    abstain: result.abstain === true || claimAbstain,
    claimAbstain,
    freshness,
    failed,
  };
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

function shouldRecallPrompt(prompt) {
  const value = String(prompt || "").trim();
  if (!value || /^\/(?:help|start|reset|new|status)\b/i.test(value)) return false;
  // Memory recall is for conversational context. Maintenance/code turns keep
  // their normal tool context and do not receive an implicit memory query.
  return !isMaintenancePrompt(value);
}

function formatMemoryContext(value, maxChars) {
  const result = parseResult(value);
  const group = result.groups?.memory && typeof result.groups.memory === "object" ? result.groups.memory : result;
  const items = Array.isArray(group.answerable)
    ? group.answerable
    : Array.isArray(group.results)
      ? group.results
      : [];
  const safeItems = items.filter((item) => {
    const citation = item?.citation && typeof item.citation === "object" ? item.citation : {};
    const status = String(item?.status || item?.evidence_status || "").toLowerCase();
    return citation.valid !== false && !["candidate", "pending", "stale", "generated", "inferred"].includes(status);
  });
  if (!safeItems.length) return undefined;
  const lines = [
    "<repository-memory-context>",
    "The following is conversation memory, not repository citation. Keep layer and status distinct.",
    "",
  ];
  for (const item of safeItems) {
    const layer = item?.memory_layer || item?.layer || "memory";
    const status = item?.status || item?.evidence_status || "verified";
    const content = text(item?.excerpt || item?.content || item?.memory?.content).trim();
    if (!content) continue;
    const id = item?.id || item?.memory_id || item?.citation?.memory_id || "unknown";
    lines.push(`- [${layer}/${status}] ${content.replace(/\s+/g, " ")} (memory_id=${id})`);
  }
  lines.push("", "</repository-memory-context>");
  return lines.join("\n").slice(0, Math.max(1000, Number(maxChars) || 12000));
}

function runRuntimeJSON(cfg, args, label) {
  const runtime = optional(cfg.runtime) || "repository-memory";
  const timeoutMs = Math.max(1000, Math.min(60000, Number(cfg.timeoutMs) || DEFAULT_TIMEOUT_MS));
  return new Promise((resolve) => {
    const child = spawn(runtime, args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    // Large repositories can legitimately produce >128 KiB JSON even for ten
    // hits.  Keep enough bytes to parse the complete document, then compact
    // the model-facing result below; truncating the raw JSON makes it invalid.
    child.stdout?.on("data", (chunk) => { stdout = (stdout + String(chunk)).slice(-1048576); });
    child.stderr?.on("data", (chunk) => { stderr = (stderr + String(chunk)).slice(-8192); });
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      resolve({ ok: false, error: `${label} timeout` });
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
          resolve({ ok: false, error: `${label} runtime returned invalid JSON` });
        }
    });
  });
}

function compactItem(item) {
  if (!item || typeof item !== "object") return item;
  const keep = [
    "id", "memory_id", "kind", "layer", "tier", "status", "accepted",
    "evidence_status", "generated", "source", "repository", "commit",
    "commit_type", "path", "line_start", "line_end", "title", "citation",
    "freshness", "support", "readback", "related", "provenance", "ref_id",
    "ref_kind",
  ];
  const result = {};
  for (const key of keep) {
    if (item[key] !== undefined) result[key] = item[key];
  }
  if (typeof item.excerpt === "string") result.excerpt = item.excerpt.slice(0, 6000);
  if (typeof item.content === "string") result.content = item.content.slice(0, 6000);
  return result;
}

function compactRuntimeResult(label, result) {
  if (!result || typeof result !== "object") return result;
  if (!label.endsWith("_search")) return result;
  const compactList = (value) => Array.isArray(value) ? value.slice(0, 10).map(compactItem) : value;
  return {
    ...result,
    verified: compactList(result.verified),
    candidates: compactList(result.candidates),
    results: compactList(result.results),
    answerable: compactList(result.answerable),
    // Full source manifests are useful to doctor, but too large for a tool
    // result.  Search keeps freshness and diagnostics, which are the routing
    // and citation decisions the Agent needs.
    sources: undefined,
  };
}

function runRecall(cfg, prompt) {
  const limit = Math.max(1, Math.min(10, Number(cfg.recallMaxResults) || 5));
  return runRuntimeJSON(cfg, ["search", prompt, "--scope", "memory", "--limit", String(limit), "--json"], "memory recall");
}

function toolResult(outcome) {
  if (!outcome.ok) {
    return {
      isError: true,
      content: [{ type: "text", text: JSON.stringify({ ok: false, error: outcome.error }) }],
      details: { ok: false, error: outcome.error },
    };
  }
  return {
    content: [{ type: "text", text: JSON.stringify(outcome.result) }],
    details: outcome.result,
  };
}

const EMPTY_PARAMETERS = { type: "object", properties: {}, additionalProperties: false };
const SEARCH_PARAMETERS = {
  type: "object",
  required: ["query"],
  properties: {
    query: { type: "string", minLength: 1, description: "Original user query; do not rewrite it into a filename." },
    scope: { type: "string", enum: ["repository", "memory", "all"], default: "repository" },
    limit: { type: "integer", minimum: 1, maximum: 50, default: 5 },
  },
  additionalProperties: false,
};
const GET_PARAMETERS = {
  type: "object",
  required: ["id"],
  properties: { id: { type: "string", minLength: 1 } },
  additionalProperties: false,
};
const TIMELINE_PARAMETERS = {
  type: "object",
  properties: {
    session_id: { type: "string" },
    limit: { type: "integer", minimum: 1, maximum: 500, default: 50 },
  },
  additionalProperties: false,
};

function registerNativeTools(api, cfg) {
  if (cfg.nativeTools === false || typeof api.registerTool !== "function") return;
  const register = (name, description, parameters, args) => {
    api.registerTool(() => ({
      name,
      label: name.replaceAll("_", " "),
      description,
      parameters,
      async execute(_toolCallId, input = {}) {
        const outcome = await runRuntimeJSON(cfg, args(input), name);
        if (outcome.ok) outcome.result = compactRuntimeResult(name, outcome.result);
        return toolResult(outcome);
      },
    }), { name });
  };
  register(
    "repository_memory_doctor",
    "Inspect repository-memory runtime, source freshness, index state, and L0-L3 capability/population. Read-only.",
    EMPTY_PARAMETERS,
    () => ["doctor", "--json"],
  );
  register(
    "repository_memory_search",
    "Search repository evidence or explicitly requested conversation memory through the shared repository-memory runtime. Read-only.",
    SEARCH_PARAMETERS,
    (input) => [
      "search", String(input.query || ""), "--scope", ["repository", "memory", "all"].includes(input.scope) ? input.scope : "repository",
      "--limit", String(Math.max(1, Math.min(10, Number(input.limit) || 5))), "--json",
    ],
  );
  register(
    "repository_memory_get",
    "Fetch a result and its citation/provenance through the shared runtime. Read-only.",
    GET_PARAMETERS,
    (input) => ["get", String(input.id || ""), "--json"],
  );
  register(
    "repository_memory_timeline",
    "Read ordered L0/L1 capture provenance for a session. This is memory provenance, not a Git citation. Read-only.",
    TIMELINE_PARAMETERS,
    (input) => ["memory-timeline", "--session-id", String(input.session_id || ""), "--limit", String(Math.max(1, Math.min(500, Number(input.limit) || 50))), "--json"],
  );
}

export default {
  id: PLUGIN_ID,
  name: "Repository Memory Auto Capture and Guard",
  description: "Audit repository-memory evidence and capture reusable shared team memory without blocking normal agent work.",
  register(api) {
    const raw = api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {};
    const cfg = {
      enabled: raw.enabled !== false,
      guardEnabled: raw.guardEnabled === true,
      runtime: raw.runtime,
      auditPath: raw.auditPath,
      repoToolPrefix: typeof raw.repoToolPrefix === "string" ? raw.repoToolPrefix : "repository-memory__",
      // Audit is the usable default: preserve routing receipts without
      // deadlocking diagnostics or ordinary work when a backend is slow.
      // ``enforcement`` is retained for config compatibility, but the guard
      // itself is advisory: it must not become a capability sandbox.
      enforcement: raw.enforcement === "enforce" ? "enforce" : "audit",
      agentIds: Array.isArray(raw.agentIds) ? raw.agentIds.filter((item) => typeof item === "string") : [],
      maxMessages: Math.max(4, Math.min(64, Number(raw.maxMessages) || 24)),
      maxMessageChars: Math.max(1000, Math.min(50000, Number(raw.maxMessageChars) || 12000)),
      timeoutMs: Math.max(1000, Math.min(60000, Number(raw.timeoutMs) || DEFAULT_TIMEOUT_MS)),
      recallEnabled: raw.recallEnabled !== false,
      recallMaxResults: Math.max(1, Math.min(10, Number(raw.recallMaxResults) || 5)),
      recallMaxChars: Math.max(1000, Math.min(20000, Number(raw.recallMaxChars) || 12000)),
      nativeTools: raw.nativeTools !== false,
    };
    if (!cfg.enabled) return;

    // MemOS exposes native OpenClaw tools in addition to lifecycle hooks.  We
    // expose the same ergonomics, but every tool delegates to this runtime's
    // CLI so MCP, CLI, and plugin results cannot diverge.
    registerNativeTools(api, cfg);

    // TencentDB's client plugin performs recall in before_prompt_build. Keep
    // the same lifecycle while delegating to the shared Python runtime, so
    // MCP, CLI, and automatic recall cannot drift into separate backends.
    if (cfg.recallEnabled) {
      api.on("before_prompt_build", async (event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const prompt = optional(event?.prompt) || optional(event?.message) || "";
        const boundaryKey = stateKey(ctx, event);
        captureBoundaryStates.set(boundaryKey, { ...captureBoundary(event, prompt), startedAt: Date.now() });
        if (!shouldRecallPrompt(prompt)) return;
        const outcome = await runRecall(cfg, prompt);
        if (!outcome.ok) {
          await appendAudit(cfg, {
            agent: optional(ctx?.agentId) || "main",
            run_id: optional(ctx?.runId) || null,
            event: "memory_recall",
            scope: "memory",
            outcome: "degraded",
            error: outcome.error,
          });
          return;
        }
        const context = formatMemoryContext(outcome.result, cfg.recallMaxChars);
        const group = outcome.result?.groups?.memory;
        await appendAudit(cfg, {
          agent: optional(ctx?.agentId) || "main",
          run_id: optional(ctx?.runId) || null,
          event: "memory_recall",
          scope: "memory",
          outcome: context ? "injected" : "empty",
          verified: Array.isArray(group?.verified) ? group.verified.length : 0,
          answerable: Array.isArray(group?.answerable) ? group.answerable.length : 0,
          retrieval_mode: outcome.result?.retrieval_mode || outcome.result?.diagnostics?.retrieval_mode || "unknown",
        });
        return context ? { prependContext: context } : undefined;
      }, { priority: 80, timeoutMs: Math.min(cfg.timeoutMs, 30000) });
    }

    // Capture boundaries are useful even when the optional audit guard is off.
    // Hosts differ in whether the original prompt arrives in before_agent_run
    // or before_prompt_build, so keep the state independently of the guard.
    api.on("before_agent_run", async (event, ctx) => {
      if (!isAllowedAgent(cfg, ctx)) return;
      const prompt = optional(event?.prompt) || optional(event?.message) || "";
      captureBoundaryStates.set(stateKey(ctx, event), { ...captureBoundary(event, prompt), startedAt: Date.now() });
    }, { priority: 95, timeoutMs: 5000 });

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
          context: false,
          searchCompleted: false,
          searchFailed: false,
          get: false,
          sync: false,
          recovery: false,
          verified: 0,
          answerable: 0,
          citations: 0,
          abstain: false,
          claimAbstain: false,
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
        const state = agentState(cfg, ctx, event);
        if (bareHostMemoryTool(toolName) && state?.strict) {
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "tool_audited", tool: toolName, reason: "bare host memory backend is outside the repository-memory evidence plane" });
        }
        if (!state?.strict) return;
        if (repoTool(cfg, toolName, "memory_search")) {
          if (!state.doctorCompleted) {
            await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "policy_warning", tool: toolName, reason: "repository search started before a successful doctor" });
          }
          state.search = true;
          return;
        }
        if (repoTool(cfg, toolName, "memory_context")) {
          state.context = true;
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
          if (!state.searchCompleted) {
            await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "policy_warning", tool: toolName, reason: "memory_get started before repository search" });
          }
          state.get = true;
          return;
        }
        const directKind = directToolKind(toolName);
        const command = directToolInput(event);
        if (directKind === "shell" && state.searchFailed && isSafeRecoveryCommand(command)) {
          state.recovery = true;
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "recovery_allowed", tool: toolName, reason: "repository-memory search failed; safe diagnostic/recovery command allowed" });
          return;
        }
        const evidenceBypass = directKind === "file-read" || (directKind === "shell" && (isEvidenceReadCommand(command) || isDestructiveCommand(command)));
        if (evidenceBypass) {
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "tool_audited", tool: toolName, reason: "repository-fact turn used direct file/shell access; final evidence remains the agent's responsibility" });
        }
      }, { priority: 100, timeoutMs: 5000 });

      api.on("after_tool_call", async (event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const toolName = optional(event?.toolName) || "unknown";
        const state = agentState(cfg, ctx, event);
        const repoSearch = repoTool(cfg, toolName, "memory_search");
        const contextTool = repoTool(cfg, toolName, "memory_context");
        const result = event?.result ?? event?.output ?? event?.resultText;
        const counts = repoSearch || contextTool ? resultCounts(result) : { verified: 0, answerable: 0, citations: 0, abstain: false, claimAbstain: false, freshness: null, failed: false };
        if ((repoSearch || contextTool) && event?.error) counts.failed = true;
        if ((repoSearch || contextTool) && state) {
          state.searchCompleted = true;
          state.searchFailed = counts.failed;
          state.verified = counts.verified;
          state.answerable = counts.answerable;
          state.citations = counts.citations;
          state.abstain = counts.abstain;
          state.claimAbstain = counts.claimAbstain;
        }
        if (state && repoTool(cfg, toolName, "memory_context")) {
          state.context = true;
          state.searchCompleted = true;
          state.searchFailed = counts.failed;
          state.verified = counts.verified;
          state.answerable = counts.answerable;
          state.citations = counts.citations;
          state.abstain = counts.abstain;
          state.claimAbstain = counts.claimAbstain;
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
          answerable: counts.answerable,
          citations: counts.citations,
          abstain: counts.abstain,
          claim_abstain: counts.claimAbstain,
          freshness: counts.freshness,
          failed: counts.failed,
          result_shape: resultShape(event?.result ?? event?.output ?? event?.resultText),
        });
      }, { priority: -100, timeoutMs: 5000 });

      api.on("before_agent_finalize", async (_event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const state = agentState(cfg, ctx, _event);
        if (!state?.strict || state.revisionRequested) return;
        const answer = finalAnswerText(_event);
        const missingReceipt = Boolean(answer) && state.verified > 0 && !hasEvidenceReceipt(answer);
        const missingRetrieval = Boolean(answer) && !state.searchCompleted && !hasExplicitAbstention(answer);
        const emptyRetrieval = Boolean(answer) && state.searchCompleted && state.verified === 0 && !state.abstain && !hasExplicitAbstention(answer);
        const unsupportedClaim = Boolean(answer) && state.verified > 0 && state.answerable === 0 && !hasExplicitAbstention(answer);
        if (missingReceipt || missingRetrieval || emptyRetrieval || unsupportedClaim) {
          await appendAudit(cfg, {
            agent: optional(ctx?.agentId) || "main",
            run_id: optional(ctx?.runId) || null,
            event: "finalize_warning",
            reason: missingReceipt ? "repository-memory answer receipt incomplete" : missingRetrieval ? "repository-fact answer had no observed shared-memory retrieval" : unsupportedClaim ? "verified citations did not support the complete claim; answer must abstain or narrow the claim" : "repository-memory answer had no verified result or explicit abstention",
            search_failed: state.searchFailed === true,
          });
        }
      }, { priority: 100, timeoutMs: 5000 });
    } else {
      // Audit-only lifecycle coverage.  These listeners never return a block
      // decision and never write memory; they make plugin behavior observable
      // on hosts where the advisory guard is intentionally disabled.
      api.on("before_tool_call", async (event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const toolName = optional(event?.toolName) || "unknown";
        if (repoTool(cfg, toolName, "memory_doctor") || repoTool(cfg, toolName, "memory_search") || repoTool(cfg, toolName, "memory_get") || repoTool(cfg, toolName, "memory_timeline")) {
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "tool_observed", tool: toolName, phase: "before" });
        }
      }, { priority: 100, timeoutMs: 5000 });
      api.on("after_tool_call", async (event, ctx) => {
        if (!isAllowedAgent(cfg, ctx)) return;
        const toolName = optional(event?.toolName) || "unknown";
        if (repoTool(cfg, toolName, "memory_doctor") || repoTool(cfg, toolName, "memory_search") || repoTool(cfg, toolName, "memory_get") || repoTool(cfg, toolName, "memory_timeline")) {
          const counts = resultCounts(event?.result ?? event?.output ?? event?.resultText);
          await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "tool_observed", tool: toolName, phase: "after", outcome: event?.error ? "error" : "completed", verified: counts.verified, citations: counts.citations, freshness: counts.freshness, latency_ms: numeric(event?.durationMs) });
        }
      }, { priority: -100, timeoutMs: 5000 });
    }

    // MemOS wires these lifecycle points for visibility and session hygiene.
    // We keep them metadata-only: session events do not create L0 records and
    // persisted tool results never enter the canonical repository implicitly.
    api.on("session_start", async (event, ctx) => {
      if (!isAllowedAgent(cfg, ctx)) return;
      await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "session_start", session_id: optional(event?.sessionId) || optional(ctx?.sessionId) || null, resumed_from: optional(event?.resumedFrom) || null });
    }, { priority: 20, timeoutMs: 5000 });
    api.on("session_end", async (event, ctx) => {
      if (!isAllowedAgent(cfg, ctx)) return;
      await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "session_end", session_id: optional(event?.sessionId) || optional(ctx?.sessionId) || null, reason: optional(event?.reason) || null });
    }, { priority: -20, timeoutMs: 5000 });
    api.on("tool_result_persist", async (event, ctx) => {
      if (!isAllowedAgent(cfg, ctx)) return;
      await appendAudit(cfg, { agent: optional(ctx?.agentId) || "main", run_id: optional(ctx?.runId) || null, event: "tool_result_persist", tool: optional(event?.toolName) || "unknown", tool_call_id: optional(event?.toolCallId) || null, result_hash: digest(event?.result ?? event?.message ?? {}), error: optional(event?.error) || null });
    }, { priority: -50, timeoutMs: 5000 });

    api.on("agent_end", (event, ctx) => {
      if (!isAllowedAgent(cfg, ctx) || event?.success === false) return;
      const boundaryKey = stateKey(ctx, event);
      const boundary = captureBoundaryStates.get(boundaryKey) || captureBoundary(event, optional(event?.prompt) || optional(event?.message) || "");
      const messages = messagesFrom(event, cfg.maxMessages, cfg.maxMessageChars, boundary);
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
        context: state?.context === true,
        get: state?.get === true,
        recovery: state?.recovery === true,
        verified: Number(state?.verified || 0),
        answerable: Number(state?.answerable || 0),
        citations: Number(state?.citations || 0),
        abstain: state?.abstain === true,
        claim_abstain: state?.claimAbstain === true,
        latency_ms: state?.startedAt ? Math.max(0, Date.now() - state.startedAt) : null,
      });
      const payload = {
        session_id: sessionId(ctx, event),
        run_id: turnId,
        agent_id: optional(ctx?.agentId) || "main",
        workspace: optional(ctx?.workspaceDir),
        messages,
        // The Node adapter may already slice by position.  A zero count keeps
        // the Python normalizer from applying the cursor a second time while
        // preserving the timestamp and original-user-text safeguards.
        original_user_message_count: 0,
        original_user_text: boundary.originalUserText,
        after_timestamp: boundary.afterTimestamp,
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
      captureBoundaryStates.delete(boundaryKey);
    });
  },
};
