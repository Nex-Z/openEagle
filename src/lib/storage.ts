import type {
  AppSettings,
  ChatMessage,
  ConversationSummary,
} from "../types/protocol";

const SETTINGS_KEY = "open-eagle/settings";
const CONVERSATIONS_KEY = "open-eagle/conversations";

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
  },
  appearance: {
    themeMode: "system",
  },
  tools: [
    {
      id: "default-shell-tool",
      name: "Shell Tool",
      description: "用于执行本地命令、脚本或自动化任务。",
      command: "pnpm run",
      enabled: true,
    },
  ],
  mcp: [
    {
      id: "default-filesystem-mcp",
      name: "Filesystem MCP",
      transport: "stdio",
      endpoint: "npx @modelcontextprotocol/server-filesystem .",
      description: "暴露当前工作区文件能力，供 Agent 调用。",
      enabled: true,
    },
  ],
  skills: [
    {
      id: "default-research-skill",
      name: "Research Assistant",
      description: "适合方案调研、知识整理与结论输出。",
      prompt: "在回答前先归纳上下文，再输出结构化结论。",
      enabled: true,
    },
  ],
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
      tools: Array.isArray(parsed.tools) ? parsed.tools : defaultSettings.tools,
      mcp: Array.isArray(parsed.mcp) ? parsed.mcp : defaultSettings.mcp,
      skills: Array.isArray(parsed.skills) ? parsed.skills : defaultSettings.skills,
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
