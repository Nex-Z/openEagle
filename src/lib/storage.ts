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
    allowedOpenIds: [],
    allowedChatIds: [],
  },
  telegram: {
    enabled: false,
    botToken: "",
    allowedUserIds: [],
    allowedChatIds: [],
  },
  im: {
    providers: [],
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
        allowedOpenIds: Array.isArray(parsed.feishu?.allowedOpenIds)
          ? parsed.feishu.allowedOpenIds
          : defaultSettings.feishu.allowedOpenIds,
        allowedChatIds: Array.isArray(parsed.feishu?.allowedChatIds)
          ? parsed.feishu.allowedChatIds
          : defaultSettings.feishu.allowedChatIds,
      },
      telegram: {
        ...defaultSettings.telegram,
        ...parsed.telegram,
        allowedUserIds: Array.isArray(parsed.telegram?.allowedUserIds)
          ? parsed.telegram.allowedUserIds
          : defaultSettings.telegram.allowedUserIds,
        allowedChatIds: Array.isArray(parsed.telegram?.allowedChatIds)
          ? parsed.telegram.allowedChatIds
          : defaultSettings.telegram.allowedChatIds,
      },
      im: {
        ...defaultSettings.im,
        ...parsed.im,
        providers: Array.isArray(parsed.im?.providers)
          ? parsed.im.providers
          : defaultSettings.im.providers,
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
  for (const summary of summaries) {
    try {
      const payload = await invoke<ConversationFilePayload>("load_conversation_file", {
        conversationId: summary.id,
      });
      const conversation = {
        summary: isConversationSummary(payload.summary) ? payload.summary : summary,
        messages: Array.isArray(payload.messages) ? payload.messages : [],
      };
      conversations.push(normalizePersistedConversation(conversation));
    } catch (err) {
      console.warn(`[storage] failed to load conversation ${summary.id}:`, err);
    }
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
