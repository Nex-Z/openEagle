import { invoke } from "@tauri-apps/api/core";
import type {
  AgentExecutionKind,
  AgentExecutionStatus,
  AgentExecutionTrace,
  AppSettings,
  AssistantMessageBlock,
  BuiltinToolConfig,
  ChatMessage,
  ConversationSummary,
  ToolConfig,
} from "../types/protocol";

const SETTINGS_KEY = "open-eagle/settings";
const CONVERSATIONS_KEY = "open-eagle/conversations";
const TRACE_TEXT_LIMIT = 12_000;
const CONVERSATION_STORE_VERSION = 1;
const LEGACY_DEFAULT_TOOL = {
  id: "default-shell-tool",
  name: "Shell Tool",
  description: "用于执行本地命令、脚本或自动化任务。",
  command: "pnpm run",
};
const LEGACY_DEFAULT_MCP_ID = "default-filesystem-mcp";
const LEGACY_DEFAULT_SKILL_ID = "default-research-skill";
const LEGACY_DEFAULT_MCP = {
  id: LEGACY_DEFAULT_MCP_ID,
  name: "Filesystem MCP",
  transport: "stdio",
  endpoint: "npx @modelcontextprotocol/server-filesystem .",
  description: "暴露当前工作区文件能力，供 Agent 调用。",
  enabled: true,
};
const LEGACY_DEFAULT_SKILL = {
  id: LEGACY_DEFAULT_SKILL_ID,
  name: "Research Assistant",
  description: "适合方案调研、知识整理与结论输出。",
  prompt: "在回答前先归纳上下文，再输出结构化结论。",
  enabled: true,
};

const DEFAULT_BUILTIN_TOOLS: BuiltinToolConfig[] = [
  {
    id: "web_search",
    name: "Web Search",
    description: "使用 DuckDuckGo 在互联网上搜索信息。",
    enabled: true,
  },
];

function createDefaultToolConfig(overrides: Partial<ToolConfig>): ToolConfig {
  return {
    id: overrides.id ?? crypto.randomUUID(),
    name: overrides.name ?? "新工具",
    description: overrides.description ?? "",
    command: overrides.command ?? "",
    cwd: overrides.cwd ?? "",
    timeoutMs: overrides.timeoutMs ?? 30_000,
    tail: overrides.tail ?? 120,
    enabled: overrides.enabled ?? true,
  };
}

function isUnmodifiedLegacyTool(tool: ToolConfig) {
  return (
    tool.id === LEGACY_DEFAULT_TOOL.id &&
    tool.name === LEGACY_DEFAULT_TOOL.name &&
    tool.description === LEGACY_DEFAULT_TOOL.description &&
    tool.command === LEGACY_DEFAULT_TOOL.command &&
    tool.cwd === "" &&
    tool.timeoutMs === 30_000 &&
    tool.tail === 120 &&
    tool.enabled === true
  );
}

function normalizeTools(tools: unknown) {
  if (!Array.isArray(tools)) {
    return defaultSettings.tools;
  }
  return tools
    .map((tool) => createDefaultToolConfig(tool as Partial<ToolConfig>))
    .filter((tool) => !isUnmodifiedLegacyTool(tool));
}

function normalizeBuiltinTools(raw: unknown): BuiltinToolConfig[] {
  if (!Array.isArray(raw)) {
    return DEFAULT_BUILTIN_TOOLS;
  }
  const defaults = new Map(DEFAULT_BUILTIN_TOOLS.map((d) => [d.id, d]));
  const result: BuiltinToolConfig[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const obj = item as Record<string, unknown>;
    const id = String(obj.id || "");
    const def = defaults.get(id);
    if (def) {
      result.push({ ...def, enabled: obj.enabled !== undefined ? Boolean(obj.enabled) : def.enabled });
      defaults.delete(id);
    }
  }
  for (const def of defaults.values()) {
    result.push(def);
  }
  return result;
}

function isUnmodifiedLegacyMcp(server: AppSettings["mcp"][number]) {
  return (
    server.id === LEGACY_DEFAULT_MCP.id &&
    server.name === LEGACY_DEFAULT_MCP.name &&
    server.transport === LEGACY_DEFAULT_MCP.transport &&
    server.endpoint === LEGACY_DEFAULT_MCP.endpoint &&
    server.description === LEGACY_DEFAULT_MCP.description &&
    server.enabled === LEGACY_DEFAULT_MCP.enabled
  );
}

function isUnmodifiedLegacySkill(skill: AppSettings["skills"][number]) {
  return (
    skill.id === LEGACY_DEFAULT_SKILL.id &&
    skill.name === LEGACY_DEFAULT_SKILL.name &&
    skill.description === LEGACY_DEFAULT_SKILL.description &&
    skill.prompt === LEGACY_DEFAULT_SKILL.prompt &&
    skill.enabled === LEGACY_DEFAULT_SKILL.enabled
  );
}

export const defaultSettings: AppSettings = {
  feishu: {
    enabled: false,
    appId: "",
    appSecret: "",
    verificationToken: "",
  },
  telegram: {
    enabled: false,
    botToken: "",
    webhookUrl: "",
  },
  agent: {
    provider: "mock",
    modelId: "gpt-5-mini",
    apiKey: "",
    baseUrl: "",
    vlProvider: "openai",
    vlModelId: "gpt-4.1-mini",
    vlApiKey: "",
    vlBaseUrl: "",
  },
  appearance: {
    themeMode: "system",
  },
  permissions: {
    mode: "default",
  },
  solo: {
    preferredDisplayIndex: 1,
  },
  tools: [],
  builtinTools: DEFAULT_BUILTIN_TOOLS,
  mcp: [],
  skills: [],
};

export type PersistedConversation = {
  summary: ConversationSummary;
  messages: ChatMessage[];
};

type ConversationIndexFile = {
  version: 1;
  conversations: ConversationSummary[];
};

type ConversationFilePayload = {
  version: 1;
  summary: ConversationSummary;
  messages: ChatMessage[];
  savedAt: string;
};

type SoloRunLogPayload = {
  requestId: string;
  path?: string;
  records?: unknown[];
  parseErrors?: number;
};

type SoloRunRecord = Record<string, unknown> & {
  event?: string;
  timestamp?: string;
  requestId?: string;
  task?: string;
  step?: number;
  action?: string;
  decision?: Record<string, unknown>;
  reason?: string;
  result?: unknown;
};

export function loadSettings(): AppSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) {
      return defaultSettings;
    }

    const parsed = JSON.parse(raw) as Partial<AppSettings>;
    return {
      ...defaultSettings,
      ...parsed,
      feishu: {
        ...defaultSettings.feishu,
        ...parsed.feishu,
      },
      telegram: {
        ...defaultSettings.telegram,
        ...parsed.telegram,
      },
      agent: {
        ...defaultSettings.agent,
        ...parsed.agent,
      },
      appearance: {
        ...defaultSettings.appearance,
        ...parsed.appearance,
      },
      permissions: {
        ...defaultSettings.permissions,
        ...parsed.permissions,
      },
      solo: {
        ...defaultSettings.solo,
        ...parsed.solo,
      },
      tools: normalizeTools(parsed.tools),
      builtinTools: normalizeBuiltinTools(parsed.builtinTools),
      mcp: Array.isArray(parsed.mcp)
        ? parsed.mcp.filter((server) => !isUnmodifiedLegacyMcp(server))
        : defaultSettings.mcp,
      skills: Array.isArray(parsed.skills)
        ? parsed.skills.filter((skill) => !isUnmodifiedLegacySkill(skill))
        : defaultSettings.skills,
    };
  } catch {
    return defaultSettings;
  }
}

export function saveSettings(settings: AppSettings) {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return isRecord(value) ? value : undefined;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function safeIdPart(value: string) {
  return value.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 96);
}

function stringifyTraceValue(value: unknown): string | undefined {
  if (value === undefined || value === null) {
    return undefined;
  }
  if (typeof value === "string") {
    return trimTraceText(value);
  }
  try {
    return trimTraceText(JSON.stringify(value, null, 2));
  } catch {
    return trimTraceText(String(value));
  }
}

function chooseLongerText(left?: string, right?: string) {
  const a = left?.trim() ?? "";
  const b = right?.trim() ?? "";
  if (!a) return b || undefined;
  if (!b) return a;
  return b.length > a.length ? b : a;
}

function trimTraceText(value: string | undefined): string | undefined {
  if (!value || value.length <= TRACE_TEXT_LIMIT) {
    return value;
  }
  return `${value.slice(0, TRACE_TEXT_LIMIT)}\n...(truncated)`;
}

function compactTraceForStorage(trace: AgentExecutionTrace): AgentExecutionTrace {
  return {
    ...trace,
    summary: trimTraceText(trace.summary),
    result: trimTraceText(trace.result),
  };
}

function compactBlocksForStorage(blocks: AssistantMessageBlock[]) {
  return blocks.map((block) =>
    block.kind === "trace"
      ? {
          ...block,
          trace: compactTraceForStorage(block.trace),
        }
      : block,
  );
}

function compactMessageForStorage(msg: ChatMessage): ChatMessage {
  if (!msg.traces && !msg.blocks && !msg.trace) {
    return msg;
  }
  return {
    ...msg,
    traces: msg.traces?.map(compactTraceForStorage),
    blocks: msg.blocks ? compactBlocksForStorage(msg.blocks) : undefined,
    trace: msg.trace ? compactTraceForStorage(msg.trace) : undefined,
  };
}

function inferRestoredTraceKind(label: string | undefined): AgentExecutionKind {
  const normalized = (label ?? "").trim().toLowerCase();
  if (normalized.startsWith("solo/") || normalized.includes("skill")) {
    return "skill";
  }
  if (normalized.includes("mcp")) {
    return "mcp";
  }
  return "tool";
}

function inferRestoredTraceStatus(message: ChatMessage): AgentExecutionStatus {
  if (message.status === "pending") {
    return "started";
  }
  if (message.status === "error") {
    return "error";
  }
  return "completed";
}

function restoreTraceOnlyToolMessage(message: ChatMessage): ChatMessage {
  if (message.blocks?.length || message.traces?.length || message.trace) {
    return message;
  }
  if (message.role !== "tool" || !message.label || message.imagePath) {
    return message;
  }

  const status = inferRestoredTraceStatus(message);
  const trace: AgentExecutionTrace = {
    id: `restored-${message.id}`,
    kind: inferRestoredTraceKind(message.label),
    name: message.label,
    status,
    summary: message.content || undefined,
    result: message.content || undefined,
    startedAt: message.createdAt,
    completedAt: status === "started" ? undefined : message.createdAt,
  };

  return {
    ...message,
    traces: [trace],
    blocks: [
      {
        id: `trace-${trace.id}`,
        kind: "trace",
        trace,
      },
    ],
  };
}

function normalizePersistedMessage(message: ChatMessage): ChatMessage {
  if (message.trace && !message.traces?.length && !message.blocks?.length) {
    const trace = { ...message.trace };
    return {
      ...message,
      traces: [trace],
      blocks: [{ id: `trace-${trace.id}`, kind: "trace", trace }],
    };
  }
  return restoreTraceOnlyToolMessage(message);
}

function normalizePersistedConversation(
  conversation: PersistedConversation,
): PersistedConversation {
  return {
    ...conversation,
    messages: conversation.messages.map(normalizePersistedMessage),
  };
}

function collectMessageTracesForStorage(message: ChatMessage): AgentExecutionTrace[] {
  const traces: AgentExecutionTrace[] = [];
  if (message.trace) {
    traces.push(message.trace);
  }
  if (message.traces) {
    traces.push(...message.traces);
  }
  if (message.blocks) {
    for (const block of message.blocks) {
      if (block.kind === "trace") {
        traces.push(block.trace);
      }
    }
  }
  return traces;
}

function createRestoredTraceMessage(params: {
  requestId: string;
  traceId: string;
  name: string;
  status: AgentExecutionStatus;
  summary?: string;
  result?: string;
  traceParams?: Record<string, unknown>;
  timestamp: string;
  label?: string;
}): ChatMessage {
  const trace: AgentExecutionTrace = {
    id: params.traceId,
    kind: inferRestoredTraceKind(params.name),
    name: params.name,
    status: params.status,
    summary: params.summary,
    params: params.traceParams,
    result: params.result,
    startedAt: params.timestamp,
    completedAt: params.status === "started" ? undefined : params.timestamp,
  };
  return {
    id: `restored-${safeIdPart(params.traceId)}`,
    requestId: params.requestId,
    role: "tool",
    label: params.label ?? params.name,
    content: params.summary ?? params.result ?? "",
    createdAt: params.timestamp,
    status: params.status === "error" ? "error" : "done",
    mode: "solo",
    traces: [trace],
    blocks: [{ id: `trace-${trace.id}`, kind: "trace", trace }],
  };
}

function createRestoredAssistantMessage(params: {
  requestId: string;
  id: string;
  label: string;
  content: string;
  timestamp: string;
  status?: ChatMessage["status"];
}): ChatMessage {
  return {
    id: params.id,
    requestId: params.requestId,
    role: "assistant",
    label: params.label,
    content: params.content,
    createdAt: params.timestamp,
    status: params.status ?? "done",
    mode: "solo",
  };
}

function createRestoredUserMessage(
  requestId: string,
  content: string,
  timestamp: string,
): ChatMessage {
  return {
    id: `restored-user-${safeIdPart(requestId)}`,
    requestId,
    role: "user",
    content,
    createdAt: timestamp,
    status: "done",
    mode: "solo",
  };
}

function collectSoloRequestIds(messages: ChatMessage[]) {
  const ids = new Set<string>();
  for (const message of messages) {
    if (message.mode === "solo" && message.requestId?.startsWith("solo-")) {
      ids.add(message.requestId);
    }
  }
  return Array.from(ids);
}

function parseStepLabel(label: string | undefined): number | undefined {
  const match = label?.match(/^第\s+(\d+)\s+步$/);
  if (!match) {
    return undefined;
  }
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : undefined;
}

function collectRepresentedSoloSteps(messages: ChatMessage[], requestId: string) {
  const steps = new Set<number>();
  for (const message of messages) {
    if (message.requestId !== requestId) {
      continue;
    }
    const labelStep = parseStepLabel(message.label);
    if (labelStep !== undefined) {
      steps.add(labelStep);
    }
    for (const trace of collectMessageTracesForStorage(message)) {
      const traceStep = asNumber(trace.params?.step);
      if (traceStep !== undefined) {
        steps.add(traceStep);
      }
      const runtimePrefix = `solo-step-${requestId}-`;
      const restoredPrefix = `${requestId}-solo-step-`;
      if (trace.id.startsWith(runtimePrefix)) {
        const value = Number(trace.id.slice(runtimePrefix.length));
        if (Number.isFinite(value)) {
          steps.add(value);
        }
      }
      if (trace.id.startsWith(restoredPrefix)) {
        const value = Number(trace.id.slice(restoredPrefix.length));
        if (Number.isFinite(value)) {
          steps.add(value);
        }
      }
    }
  }
  return steps;
}

function hasTerminalSoloMessage(messages: ChatMessage[], requestId: string) {
  return messages.some(
    (message) =>
      message.requestId === requestId &&
      (message.label === "SOLO completed" ||
        message.label === "SOLO aborted" ||
        message.label === "SOLO error" ||
        message.label === "SOLO paused"),
  );
}

function normalizeSoloRunRecords(records: unknown[]): SoloRunRecord[] {
  return records.filter(isRecord).map((record) => record as SoloRunRecord);
}

function loggedSoloSteps(records: SoloRunRecord[]) {
  const steps = new Set<number>();
  for (const record of records) {
    const step = asNumber(record.step);
    if (step !== undefined) {
      steps.add(step);
    }
  }
  return steps;
}

function hasTerminalSoloRecord(records: SoloRunRecord[]) {
  return records.some((record) =>
    ["aborted", "error", "paused"].includes(asString(record.event) ?? ""),
  );
}

function shouldRecoverSoloRun(
  messages: ChatMessage[],
  requestId: string,
  records: SoloRunRecord[],
) {
  if (records.length === 0) {
    return false;
  }
  const represented = collectRepresentedSoloSteps(messages, requestId);
  for (const step of loggedSoloSteps(records)) {
    if (!represented.has(step)) {
      return true;
    }
  }
  const requestMessages = messages.filter((message) => message.requestId === requestId);
  const hasOnlyStarter =
    requestMessages.length > 0 &&
    !requestMessages.some(
      (message) =>
        message.role === "assistant" ||
        (message.role === "tool" && message.label && message.label !== "SOLO/agent"),
    );
  return (
    hasOnlyStarter ||
    (hasTerminalSoloRecord(records) && !hasTerminalSoloMessage(messages, requestId))
  );
}

function decisionVisibleText(decision: Record<string, unknown>, action: string) {
  const finishReport = asString(decision.finish_report);
  const agentMessage = asString(decision.agent_message);
  const thought = asString(decision.thought_summary);
  const expected = asString(decision.progress) ?? asString(decision.expected_outcome);
  const findings = asStringArray(decision.findings);
  let content =
    action === "finish"
      ? chooseLongerText(finishReport, agentMessage)
      : agentMessage?.trim();
  content = content || thought || expected || "步骤已更新。";
  if (action === "finish" && findings.length > 0 && !content.includes(findings[0])) {
    content += `\n\n发现:\n${findings.map((finding) => `- ${finding}`).join("\n")}`;
  }
  return content;
}

function actionResultSummary(record: SoloRunRecord) {
  const action = asString(record.action) ?? "unknown";
  const result = asRecord(record.result);
  const ok = result?.ok ?? result?.success;
  const exitCode = result?.exitCode;
  const outputTail = asString(result?.outputTail);
  const parts = [`动作结果: ${action}`];
  if (ok !== undefined) {
    parts.push(`ok=${String(ok)}`);
  }
  if (exitCode !== undefined) {
    parts.push(`exitCode=${String(exitCode)}`);
  }
  if (outputTail) {
    parts.push(outputTail);
  }
  return parts.join("\n");
}

function buildMessagesFromSoloRun(
  requestId: string,
  records: SoloRunRecord[],
  existingSteps: Set<number>,
) {
  const messages: ChatMessage[] = [];
  const emittedAssistantSteps = new Set<number>();

  for (const record of records) {
    const event = asString(record.event);
    const timestamp = asString(record.timestamp) ?? new Date().toISOString();
    const step = asNumber(record.step);

    if (event === "run_started" && asString(record.task)) {
      messages.push(createRestoredUserMessage(requestId, asString(record.task)!, timestamp));
      continue;
    }

    if (event === "agent_start") {
      messages.push(
        createRestoredTraceMessage({
          requestId,
          traceId: `${requestId}-solo-trace-0-agent-started`,
          name: "SOLO/agent",
          status: "completed",
          summary: "Agent 开始自主决策执行任务...",
          timestamp,
        }),
      );
      continue;
    }

    if (event === "decision") {
      const decision = asRecord(record.decision);
      if (!decision || step === undefined || existingSteps.has(step)) {
        continue;
      }
      const action = asString(decision.action) ?? "unknown";
      const actionArgs = asRecord(decision.action_args) ?? {};
      const thought = asString(decision.thought_summary);
      const expected = asString(decision.progress) ?? asString(decision.expected_outcome);
      messages.push(
        createRestoredTraceMessage({
          requestId,
          traceId: `${requestId}-solo-trace-${Math.max(0, step - 1)}-decision-completed`,
          name: "SOLO/decision",
          status: "completed",
          summary: `视觉决策: ${action}`,
          traceParams: {
            step,
            thought,
            expected_outcome: expected,
            screen_state: asString(decision.screen_state),
            confidence: asNumber(decision.confidence),
          },
          result: stringifyTraceValue(decision),
          timestamp,
        }),
      );
      messages.push(
        createRestoredAssistantMessage({
          requestId,
          id: `restored-solo-step-${safeIdPart(requestId)}-${step}`,
          label: `第 ${step} 步`,
          content: decisionVisibleText(decision, action),
          timestamp,
        }),
      );
      emittedAssistantSteps.add(step);
      messages.push(
        createRestoredTraceMessage({
          requestId,
          traceId: `${requestId}-solo-step-${step}`,
          name: action,
          status: "started",
          summary: expected,
          traceParams: {
            step,
            action,
            actionArgs,
            screenshotPath: asString(record.screenshotPath),
          },
          timestamp,
          label: action,
        }),
      );
      continue;
    }

    if (event === "decision_parse_recovery") {
      if (step !== undefined && existingSteps.has(step)) {
        continue;
      }
      messages.push(
        createRestoredTraceMessage({
          requestId,
          traceId: `${requestId}-solo-trace-${step ?? 0}-decision_repair-completed`,
          name: "SOLO/decision_repair",
          status: "completed",
          summary: "决策 JSON 已修复并恢复解析。",
          traceParams: {
            step,
            usedFallback: record.usedFallback,
          },
          result: stringifyTraceValue({
            rawOutput: record.rawOutput,
            repairOutput: record.repairOutput,
          }),
          timestamp,
        }),
      );
      continue;
    }

    if (event === "action_result") {
      if (step !== undefined && existingSteps.has(step)) {
        continue;
      }
      const action = asString(record.action) ?? "unknown";
      if (step !== undefined && !emittedAssistantSteps.has(step)) {
        messages.push(
          createRestoredAssistantMessage({
            requestId,
            id: `restored-solo-action-${safeIdPart(requestId)}-${step}`,
            label: `第 ${step} 步`,
            content: `执行动作: ${action}`,
            timestamp,
          }),
        );
        emittedAssistantSteps.add(step);
      }
      messages.push(
        createRestoredTraceMessage({
          requestId,
          traceId: `${requestId}-solo-trace-${step ?? 0}-step_result-${
            record.semanticSuccess === false ? "error" : "completed"
          }`,
          name: "SOLO/step_result",
          status: record.semanticSuccess === false ? "error" : "completed",
          summary: actionResultSummary(record),
          traceParams: {
            step,
            action,
            batchIndex: record.batchIndex ?? null,
          },
          result: stringifyTraceValue(record.result),
          timestamp,
        }),
      );
      continue;
    }

    if (event && ["aborted", "error", "paused"].includes(event)) {
      const reason =
        asString(record.reason) ??
        `SOLO ${event}`;
      messages.push(
        createRestoredAssistantMessage({
          requestId,
          id: `restored-solo-${safeIdPart(requestId)}-${event}`,
          label: `SOLO ${event}`,
          content: reason,
          timestamp,
          status: event === "error" ? "error" : "done",
        }),
      );
    }
  }

  return messages;
}

function soloMessageRecoveryKey(message: ChatMessage) {
  const requestId = message.requestId ?? "";
  if (!requestId) {
    return `id:${message.id}`;
  }
  const step = parseStepLabel(message.label);
  if (message.role === "assistant" && step !== undefined) {
    return `${requestId}|assistant-step|${step}`;
  }
  const trace = collectMessageTracesForStorage(message)[0];
  if (trace) {
    const traceStep = asNumber(trace.params?.step);
    return `${requestId}|trace|${trace.name}|${trace.status}|${traceStep ?? ""}|${
      trace.params?.batchIndex ?? ""
    }`;
  }
  return `${requestId}|${message.role}|${message.label ?? ""}|${message.content.slice(0, 120)}`;
}

function mergeRecoveredMessages(
  existing: ChatMessage[],
  recovered: ChatMessage[],
) {
  const seen = new Set(existing.map(soloMessageRecoveryKey));
  const next = [...existing];
  for (const message of recovered) {
    const key = soloMessageRecoveryKey(message);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    next.push(message);
  }
  return next.sort(
    (left, right) =>
      new Date(left.createdAt).getTime() - new Date(right.createdAt).getTime(),
  );
}

function refreshSummaryUpdatedAt(conversation: PersistedConversation) {
  const latest = conversation.messages.reduce<string | null>((current, message) => {
    if (!current) {
      return message.createdAt;
    }
    return new Date(message.createdAt).getTime() > new Date(current).getTime()
      ? message.createdAt
      : current;
  }, null);
  if (!latest || latest === conversation.summary.updatedAt) {
    return conversation;
  }
  return {
    ...conversation,
    summary: {
      ...conversation.summary,
      updatedAt: latest,
    },
  };
}

async function recoverPersistedConversation(
  conversation: PersistedConversation,
): Promise<{ conversation: PersistedConversation; recovered: boolean }> {
  let current = normalizePersistedConversation(conversation);
  if (!isTauriRuntime()) {
    return { conversation: current, recovered: false };
  }

  let recovered = false;
  for (const requestId of collectSoloRequestIds(current.messages)) {
    try {
      const payload = await invoke<SoloRunLogPayload>("load_solo_run_log", {
        requestId,
      });
      const records = normalizeSoloRunRecords(payload.records ?? []);
      if (!shouldRecoverSoloRun(current.messages, requestId, records)) {
        continue;
      }
      const existingSteps = collectRepresentedSoloSteps(current.messages, requestId);
      const restoredMessages = buildMessagesFromSoloRun(requestId, records, existingSteps);
      const nextMessages = mergeRecoveredMessages(current.messages, restoredMessages);
      if (nextMessages.length !== current.messages.length) {
        current = refreshSummaryUpdatedAt({
          ...current,
          messages: nextMessages,
        });
        recovered = true;
      }
    } catch (err) {
      console.warn(`[storage] failed to recover solo run ${requestId}:`, err);
    }
  }
  return { conversation: current, recovered };
}

function isConversationSummary(value: unknown): value is ConversationSummary {
  if (!value || typeof value !== "object") {
    return false;
  }
  const summary = value as Partial<ConversationSummary>;
  return (
    typeof summary.id === "string" &&
    typeof summary.title === "string" &&
    typeof summary.updatedAt === "string"
  );
}

function isPersistedConversation(value: unknown): value is PersistedConversation {
  if (!value || typeof value !== "object") {
    return false;
  }
  const conversation = value as Partial<PersistedConversation>;
  return isConversationSummary(conversation.summary) && Array.isArray(conversation.messages);
}

function loadLocalStorageConversations(): PersistedConversation[] {
  try {
    const raw = localStorage.getItem(CONVERSATIONS_KEY);
    if (!raw) {
      return [];
    }

    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }

    // Validate each conversation; skip corrupted ones.
    return parsed.filter(isPersistedConversation).map(normalizePersistedConversation);
  } catch {
    return [];
  }
}

let localStorageConversationCache: PersistedConversation[] | null = null;

function getLocalStorageConversationCache() {
  if (!localStorageConversationCache) {
    localStorageConversationCache = loadLocalStorageConversations();
  }
  return localStorageConversationCache;
}

/** Last-resort compaction for browsers that hit localStorage quota. */
function stripExecutionFields(msg: ChatMessage): ChatMessage {
  if (!msg.traces && !msg.blocks && !msg.trace) {
    return msg;
  }
  return {
    ...msg,
    traces: undefined,
    blocks: undefined,
    trace: undefined,
  };
}

/** Estimate serialized size in bytes (rough UTF-8). */
function estimateSize(obj: unknown): number {
  return new TextEncoder().encode(JSON.stringify(obj)).length;
}

/** Max localStorage budget: 4 MB (leave headroom under the ~5 MB limit). */
const STORAGE_BUDGET = 4 * 1024 * 1024;

function saveLocalStorageConversations(
  conversations: PersistedConversation[],
) {
  localStorageConversationCache = conversations;
  const compacted = conversations.map((c) => ({
    ...c,
    messages: c.messages.map(compactMessageForStorage),
  }));

  try {
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(compacted));
    return;
  } catch (err) {
    if (!isQuotaExceeded(err)) {
      console.warn("[storage] save failed (non-quota):", err);
      return;
    }
    console.warn("[storage] quota exceeded, pruning…");
  }

  // Quota exceeded — progressively prune until it fits.
  let pruned = compacted;

  // Phase 1: drop execution blocks/traces from oldest conversations first.
  for (let i = 0; i < pruned.length && estimateSize(pruned) > STORAGE_BUDGET; i++) {
    pruned[i] = {
      ...pruned[i],
      messages: pruned[i].messages.map(stripExecutionFields),
    };
  }

  // Phase 2: truncate long assistant content in oldest conversations
  for (let i = 0; i < pruned.length && estimateSize(pruned) > STORAGE_BUDGET; i++) {
    pruned[i] = {
      ...pruned[i],
      messages: pruned[i].messages.map((m) =>
        m.role === "assistant" && m.content.length > 2000
          ? { ...m, content: m.content.slice(0, 2000) + "\n…(truncated)" }
          : m,
      ),
    };
  }

  // Phase 3: remove oldest conversations entirely
  while (pruned.length > 1 && estimateSize(pruned) > STORAGE_BUDGET) {
    pruned.shift();
  }

  try {
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(pruned));
  } catch (err) {
    console.error("[storage] save failed even after pruning:", err);
  }
}

function toConversationFilePayload(
  conversation: PersistedConversation,
): ConversationFilePayload {
  return {
    version: CONVERSATION_STORE_VERSION,
    summary: conversation.summary,
    messages: conversation.messages,
    savedAt: new Date().toISOString(),
  };
}

function updateLocalStorageIndex(summaries: ConversationSummary[]) {
  const cache = getLocalStorageConversationCache();
  const byId = new Map(cache.map((conversation) => [conversation.summary.id, conversation]));
  const next = summaries.map((summary) => ({
    summary,
    messages: byId.get(summary.id)?.messages ?? [],
  }));
  saveLocalStorageConversations(next);
}

function saveLocalStorageConversation(conversation: PersistedConversation) {
  const cache = getLocalStorageConversationCache();
  const index = cache.findIndex((item) => item.summary.id === conversation.summary.id);
  const next =
    index >= 0
      ? cache.map((item, itemIndex) => (itemIndex === index ? conversation : item))
      : [conversation, ...cache];
  saveLocalStorageConversations(next);
}

function deleteLocalStorageConversation(conversationId: string) {
  const next = getLocalStorageConversationCache().filter(
    (conversation) => conversation.summary.id !== conversationId,
  );
  saveLocalStorageConversations(next);
}

async function saveConversationIndexFile(summaries: ConversationSummary[]) {
  await invoke("save_conversation_index", {
    index: {
      version: CONVERSATION_STORE_VERSION,
      conversations: summaries,
    } satisfies ConversationIndexFile,
  });
}

async function saveConversationFile(conversation: PersistedConversation) {
  await invoke("save_conversation_file", {
    conversation: toConversationFilePayload(conversation),
  });
}

async function migrateLocalStorageConversations() {
  const legacyConversations = loadLocalStorageConversations();
  if (legacyConversations.length === 0) {
    return [];
  }

  const saved: PersistedConversation[] = [];
  for (const conversation of legacyConversations) {
    try {
      await saveConversationFile(conversation);
      saved.push(conversation);
    } catch (err) {
      console.warn(
        `[storage] failed to migrate conversation ${conversation.summary.id}:`,
        err,
      );
    }
  }

  if (saved.length > 0) {
    await saveConversationIndexFile(saved.map((conversation) => conversation.summary));
    localStorage.removeItem(CONVERSATIONS_KEY);
  }
  return saved;
}

async function loadFileConversations() {
  const index = await invoke<ConversationIndexFile>("load_conversation_index");
  const summaries = Array.isArray(index.conversations)
    ? index.conversations.filter(isConversationSummary)
    : [];

  if (summaries.length === 0) {
    const migrated = await migrateLocalStorageConversations();
    if (migrated.length > 0) {
      return migrated;
    }
  }

  const conversations: PersistedConversation[] = [];
  const recoveredConversations: PersistedConversation[] = [];
  for (const summary of summaries) {
    try {
      const payload = await invoke<ConversationFilePayload>("load_conversation_file", {
        conversationId: summary.id,
      });
      const conversation = {
        summary: isConversationSummary(payload.summary) ? payload.summary : summary,
        messages: Array.isArray(payload.messages) ? payload.messages : [],
      };
      const recovered = await recoverPersistedConversation(conversation);
      conversations.push(recovered.conversation);
      if (recovered.recovered) {
        recoveredConversations.push(recovered.conversation);
      }
    } catch (err) {
      console.warn(`[storage] failed to load conversation ${summary.id}:`, err);
    }
  }
  if (recoveredConversations.length > 0) {
    for (const conversation of recoveredConversations) {
      await saveConversationFile(conversation);
    }
    await saveConversationIndexFile(conversations.map((conversation) => conversation.summary));
  }
  return conversations;
}

export async function loadPersistedConversations(): Promise<PersistedConversation[]> {
  if (!isTauriRuntime()) {
    const conversations = loadLocalStorageConversations();
    localStorageConversationCache = conversations;
    return conversations;
  }

  try {
    return await loadFileConversations();
  } catch (err) {
    console.warn("[storage] failed to load file-backed conversations:", err);
    return [];
  }
}

export async function savePersistedConversationIndex(
  summaries: ConversationSummary[],
) {
  if (!isTauriRuntime()) {
    updateLocalStorageIndex(summaries);
    return;
  }
  await saveConversationIndexFile(summaries);
}

export async function savePersistedConversation(
  conversation: PersistedConversation,
) {
  if (!isTauriRuntime()) {
    saveLocalStorageConversation(conversation);
    return;
  }
  await saveConversationFile(conversation);
}

export async function deletePersistedConversation(conversationId: string) {
  if (!isTauriRuntime()) {
    deleteLocalStorageConversation(conversationId);
    return;
  }
  await invoke("delete_conversation_file", { conversationId });
}

export async function savePersistedConversations(
  conversations: PersistedConversation[],
) {
  if (!isTauriRuntime()) {
    saveLocalStorageConversations(conversations);
    return;
  }
  for (const conversation of conversations) {
    await saveConversationFile(conversation);
  }
  await saveConversationIndexFile(conversations.map((conversation) => conversation.summary));
}

function isQuotaExceeded(err: unknown): boolean {
  if (err instanceof DOMException) {
    // Chromium, Firefox, Safari
    return (
      err.code === 22 ||
      err.code === 1014 ||
      err.name === "QuotaExceededError" ||
      err.name === "NS_ERROR_DOM_QUOTA_REACHED"
    );
  }
  return false;
}
