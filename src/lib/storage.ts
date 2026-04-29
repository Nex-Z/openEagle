import type {
  AppSettings,
  BuiltinToolConfig,
  ChatMessage,
  ConversationSummary,
  ToolConfig,
} from "../types/protocol";

const SETTINGS_KEY = "open-eagle/settings";
const CONVERSATIONS_KEY = "open-eagle/conversations";
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

export function loadPersistedConversations(): PersistedConversation[] {
  try {
    const raw = localStorage.getItem(CONVERSATIONS_KEY);
    if (!raw) {
      return [];
    }

    return JSON.parse(raw) as PersistedConversation[];
  } catch {
    return [];
  }
}

export function savePersistedConversations(
  conversations: PersistedConversation[],
) {
  localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(conversations));
}
