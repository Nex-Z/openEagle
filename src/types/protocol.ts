export type ConnectionPhase =
  | "idle"
  | "starting"
  | "ready"
  | "connecting"
  | "connected"
  | "error"
  | "disconnected";

export type ThemeMode = "dark" | "light" | "system";

export interface Envelope<TPayload = Record<string, unknown>> {
  type: string;
  requestId: string;
  conversationId: string;
  payload: TPayload;
  timestamp: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  requestId?: string;
  status?: "pending" | "done" | "error";
}

export interface FeishuSettings {
  enabled: boolean;
  appId: string;
  appSecret: string;
  verificationToken: string;
}

export interface AgentSettings {
  provider: "mock" | "openai" | "openai-like";
  modelId: string;
  apiKey: string;
  baseUrl: string;
}

export interface AppearanceSettings {
  themeMode: ThemeMode;
}

export interface AppSettings {
  feishu: FeishuSettings;
  agent: AgentSettings;
  appearance: AppearanceSettings;
}

export interface ConversationSummary {
  id: string;
  title: string;
  updatedAt: string;
}

export interface BackendState {
  phase: ConnectionPhase;
  port: number | null;
  message: string;
}

export interface StatusPayload {
  stage: "booting" | "connected" | "thinking" | "idle";
  detail?: string;
}

export interface ErrorPayload {
  message: string;
  code?: string;
}
