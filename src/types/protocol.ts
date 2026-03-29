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
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  createdAt: string;
  requestId?: string;
  status?: "pending" | "done" | "error";
  traces?: AgentExecutionTrace[];
  trace?: AgentExecutionTrace;
  blocks?: AssistantMessageBlock[];
}

export type AgentExecutionKind = "tool" | "mcp" | "skill";

export type AgentExecutionStatus = "started" | "completed" | "error";

export interface AgentExecutionTrace {
  id: string;
  kind: AgentExecutionKind;
  name: string;
  status: AgentExecutionStatus;
  summary?: string;
  params?: Record<string, unknown>;
  result?: string;
  startedAt: string;
  completedAt?: string;
}

export interface AssistantTextBlock {
  id: string;
  kind: "text";
  content: string;
  status?: "pending" | "done";
}

export interface AssistantTraceBlock {
  id: string;
  kind: "trace";
  trace: AgentExecutionTrace;
}

export type AssistantMessageBlock = AssistantTextBlock | AssistantTraceBlock;

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

export interface ToolConfig {
  id: string;
  name: string;
  description: string;
  command: string;
  enabled: boolean;
}

export interface McpServerConfig {
  id: string;
  name: string;
  transport: "stdio" | "http";
  endpoint: string;
  description: string;
  enabled: boolean;
}

export interface SkillConfig {
  id: string;
  name: string;
  description: string;
  prompt: string;
  enabled: boolean;
}

export interface AppSettings {
  feishu: FeishuSettings;
  agent: AgentSettings;
  appearance: AppearanceSettings;
  tools: ToolConfig[];
  mcp: McpServerConfig[];
  skills: SkillConfig[];
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
