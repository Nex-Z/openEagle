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
