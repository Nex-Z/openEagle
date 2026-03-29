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
  mode?: "chat" | "solo";
  imagePath?: string;
  label?: string;
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
  vlProvider: "openai" | "openai-like";
  vlModelId: string;
  vlApiKey: string;
  vlBaseUrl: string;
}

export interface AppearanceSettings {
  themeMode: ThemeMode;
}

export interface SoloSettings {
  preferredDisplayIndex: number;
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
  solo: SoloSettings;
  tools: ToolConfig[];
  mcp: McpServerConfig[];
  skills: SkillConfig[];
}

export interface SoloDisplayOption {
  index: number;
  label: string;
  left: number;
  top: number;
  width: number;
  height: number;
  isPrimary: boolean;
  isSelected: boolean;
  previewPath?: string;
  capturedAt?: string;
}

export type SoloRunState =
  | "idle"
  | "running"
  | "paused"
  | "waiting_user_confirmation"
  | "completed"
  | "aborted"
  | "error";

export interface SoloStatusPayload {
  state: SoloRunState;
  detail?: string;
  stepCount: number;
  maxSteps: number;
  lastAction?: string;
  lastScreenshotAt?: string;
  startedAt?: string;
  completedAt?: string;
}

export interface SoloStepPayload {
  stepIndex: number;
  action: string;
  actionArgs?: Record<string, unknown>;
  thoughtSummary: string;
  expectedOutcome?: string;
  screenshotPath?: string;
  timestamp: string;
}

export interface SoloConfirmationPayload {
  stepIndex: number;
  reason: string;
  action: string;
  actionArgs?: Record<string, unknown>;
  thoughtSummary: string;
}

export interface SoloControlPayload {
  action:
    | "pause"
    | "resume"
    | "stop"
    | "confirm_allow"
    | "confirm_reject"
    | "step_result";
  soloRequestId?: string;
  result?: Record<string, unknown>;
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
