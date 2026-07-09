export type ConnectionPhase =
  | "idle"
  | "starting"
  | "ready"
  | "connecting"
  | "connected"
  | "error"
  | "disconnected";

export type ThemeMode = "dark" | "light" | "system";
export type PermissionMode = "default" | "all";

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
  completedAt?: string;
  mode?: "solo";
  imagePath?: string;
  attachments?: AttachmentRef[];
  label?: string;
  requestId?: string;
  status?: "pending" | "done" | "error";
  traces?: AgentExecutionTrace[];
  trace?: AgentExecutionTrace;
  blocks?: AssistantMessageBlock[];
  tokenUsage?: TokenUsageSummary;
}

export interface TokenUsageSummary {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  calls: number;
}

export interface TokenUsageDay extends TokenUsageSummary {
  date: string;
}

export interface TokenUsageModel extends TokenUsageSummary {
  provider: string;
  model: string;
}

export interface TokenUsageRequest extends TokenUsageSummary {
  requestId: string;
  conversationId: string;
  source: string;
  models: string[];
  updatedAt: string;
}

export interface TokenUsageDashboard {
  total: TokenUsageSummary;
  today: TokenUsageSummary;
  days: TokenUsageDay[];
  models: TokenUsageModel[];
  recentRequests: TokenUsageRequest[];
}

export type AgentExecutionKind = "tool" | "mcp" | "skill" | "agent";

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
  purpose?: "progress" | "final";
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
  allowedOpenIds: string[];
  allowedChatIds: string[];
  status?: string;
}

export interface TelegramSettings {
  enabled: boolean;
  botToken: string;
  webhookUrl: string;
  allowedUserIds: string[];
  allowedChatIds: string[];
  status?: string;
}

export interface WechatSettings {
  enabled: boolean;
  accountId: string;
  baseUrl: string;
  botType: string;
  allowedUserIds: string[];
  allowedChatIds: string[];
  status?: string;
}

export interface AttachmentRef {
  id: string;
  name: string;
  mimeType: string;
  size: number;
  kind: "image" | "file" | "audio" | "video" | "unknown";
  source: "local" | "remote" | "generated";
  localPath?: string;
  remoteMeta?: Record<string, unknown>;
  status?: "pending" | "ready" | "error";
  error?: string;
  contentBase64?: string;
  previewUrl?: string;
}

export type ImChannel = "feishu" | "telegram" | "wechat";

export interface ImProviderSettings {
  id: string;
  type: ImChannel;
  name: string;
  enabled: boolean;
  appId?: string;
  appSecret?: string;
  botToken?: string;
  accountId?: string;
  baseUrl?: string;
  botType?: string;
  allowedOpenIds?: string[];
  allowedUserIds?: string[];
  allowedChatIds: string[];
}

export interface ImSettings {
  providers: ImProviderSettings[];
}

export interface AgentSettings {
  provider: "mock" | "openai" | "openai-like" | "anthropic";
  modelId: string;
  apiKey: string;
  baseUrl: string;
  vlProvider: "openai" | "openai-like" | "anthropic";
  vlModelId: string;
  vlApiKey: string;
  vlBaseUrl: string;
}

export interface AppearanceSettings {
  themeMode: ThemeMode;
}

export interface PermissionSettings {
  mode: PermissionMode;
}

export type ToolMessageMode = "placeholder" | "remove";

export interface ContextSettings {
  enabled: boolean;
  maxInputTokens: number;
  conversationTurnLimit: number;
  preserveRecentMessages: number;
  imIdleCleanupMinutes: number;
  toolMessageMode: ToolMessageMode;
  aiSummaryEnabled: boolean;
  snapshotOnCompaction: boolean;
  summaryCharLimit: number;
  toolResultCharLimit: number;
  middleMessageCharLimit: number;
}

export interface SoloSettings {
  preferredDisplayIndex: number;
}

export interface QuickAssistantSettings {
  enabled: boolean;
  hotkey: string;
  autoReadSelection: boolean;
}

export interface ToolConfig {
  id: string;
  name: string;
  description: string;
  command: string;
  cwd: string;
  timeoutMs: number;
  tail: number;
  enabled: boolean;
}

export interface McpServerConfig {
  id: string;
  name: string;
  transport: "stdio" | "http" | "sse" | "streamable-http";
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

export interface BuiltinToolConfig {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
}

export interface WebSearchSettings {
  provider: "tavily" | "disabled";
  apiKey: string;
  searchDepth: "basic" | "advanced";
  maxResults: number;
}

export interface VoiceInputSettings {
  enabled: boolean;
  apiKey: string;
  baseUrl: string;
  modelId: string;
  maxDurationSeconds: number;
}

export interface AppSettings {
  feishu: FeishuSettings;
  telegram: TelegramSettings;
  wechat: WechatSettings;
  im: ImSettings;
  agent: AgentSettings;
  appearance: AppearanceSettings;
  permissions: PermissionSettings;
  context: ContextSettings;
  solo: SoloSettings;
  quickAssistant: QuickAssistantSettings;
  tools: ToolConfig[];
  builtinTools: BuiltinToolConfig[];
  webSearch: WebSearchSettings;
  voiceInput: VoiceInputSettings;
  mcp: McpServerConfig[];
  skills: SkillConfig[];
}

export interface MemoryProfile {
  content: string;
  updatedAt: string;
  manualUpdatedAt?: string;
}

export type MemoryNoteStatus = "active" | "archived";

export interface MemoryNote {
  id: string;
  text: string;
  tags: string[];
  source: string;
  confidence: number;
  status: MemoryNoteStatus;
  createdAt: string;
  updatedAt: string;
}

export interface AgentSoul {
  core: string;
  sideNotes: string;
  updatedAt: string;
  sideNotesUpdatedAt?: string;
}

export interface MemoryAudit {
  id: string;
  action: string;
  targetKind: string;
  targetId?: string;
  summary?: string;
  source: string;
  createdAt: string;
}

export interface MemoryEvent {
  id: string;
  source: string;
  conversationId?: string;
  requestId?: string;
  summary?: string;
  content?: string;
  payload?: Record<string, unknown>;
  createdAt: string;
}

export interface MemoryState {
  profile: MemoryProfile;
  notes: MemoryNote[];
  agentSoul: AgentSoul;
  audit: MemoryAudit[];
  events: MemoryEvent[];
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

export interface SoloScreenshotPayload {
  path: string;
  width?: number;
  height?: number;
  capturedAt?: string;
  contentHash?: string;
  displayIndex?: number;
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
  logPath?: string;
  startedAt?: string;
  completedAt?: string;
}

export interface SoloStepVisualPayload {
  kind: "point" | "scroll" | "keyboard" | "command" | "navigation" | "wait" | "none";
  x?: number;
  y?: number;
  displayIndex?: number;
  coordinateSpace?: "screen" | "screenshot" | "unknown";
  screenshotPath?: string;
  screenshotX?: number;
  screenshotY?: number;
  screenshotWidth?: number;
  screenshotHeight?: number;
  displayText?: string;
  targetLabel?: string;
  safeArgsPreview?: Record<string, unknown>;
}

export interface SoloStepPayload {
  stepIndex: number;
  action: string;
  actionArgs?: Record<string, unknown>;
  thoughtSummary: string;
  agentMessage?: string;
  expectedOutcome?: string;
  screenshotPath?: string;
  timestamp: string;
  findings?: string[];
  confidence?: number;
  screenState?: string;
  visual?: SoloStepVisualPayload;
}

export interface SoloConfirmationPayload {
  stepIndex: number;
  riskLevel?: "confirm";
  reason: string;
  action: string;
  actionArgs?: Record<string, unknown>;
  thoughtSummary: string;
  visual?: SoloStepVisualPayload;
}

export interface ToolConfirmationPayload {
  confirmationId: string;
  riskLevel: "confirm";
  kind: "tool" | "mcp" | "skill";
  name: string;
  reason: string;
  params?: Record<string, unknown>;
  createdAt: string;
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

export interface SoloPlanItem {
  index: number;
  action: string;
  description: string;
  status: "pending" | "in_progress" | "completed" | "failed" | "skipped";
}

export interface SoloPlanStatus {
  items: SoloPlanItem[];
  taskAnalysis: string;
  alternative: string;
  agentMessage: string;
  replanCount: number;
}

export interface SoloOverlayPlanItem {
  index: number;
  status: SoloPlanItem["status"];
  text: string;
}

export type SoloOverlayControlAction =
  | "pause"
  | "resume"
  | "stop"
  | "confirm_allow"
  | "confirm_reject"
  | "open_main"
  | "dismiss";

export interface SoloOverlayControlPayload {
  action: SoloOverlayControlAction;
}

export interface SoloOverlayState {
  state: SoloRunState;
  title: string;
  detail: string;
  stepText: string;
  stepLabel?: string;
  historyText: string;
  stepCount: number;
  maxSteps: number;
  planItems?: SoloOverlayPlanItem[];
  lastAction?: string;
  confirmationAction?: string;
  confirmationReason?: string;
}

export type QuickContextKind = "selection" | "screenshot" | "manual";

export interface QuickContextItem {
  id: string;
  kind: QuickContextKind;
  title: string;
  content?: string;
  attachmentId?: string;
  createdAt: string;
}

export interface QuickAssistantSubmitPayload {
  quickRequestId: string;
  content: string;
  actionId?: string;
  contextItems: QuickContextItem[];
  attachments?: AttachmentRef[];
  createdAt: string;
}

export interface QuickAssistantRuntimeState {
  quickRequestId?: string;
  requestId?: string;
  status?: "idle" | "pending" | "done" | "error" | "solo";
  content?: string;
  detail?: string;
  backendReady?: boolean;
  backendDetail?: string;
  attachments?: AttachmentRef[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  updatedAt: string;
}

export interface IMStatusPayload {
  provider: "feishu" | "telegram" | "wechat";
  state: "disabled" | "starting" | "connected" | "error";
  detail?: string;
  lastBlockedOpenId?: string;
  lastBlockedChatId?: string;
}

export interface WechatBindStatusPayload {
  state:
    | "qrcode"
    | "waiting"
    | "bound"
    | "cancelled"
    | "unbound"
    | "error";
  message: string;
  qrcodeUrl?: string;
  accountId?: string;
  userId?: string;
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

export interface ScheduledTask {
  id: string;
  name: string;
  prompt: string;
  scheduleExpr: string;
  scheduleType: "cron" | "interval" | "date";
  enabled: boolean;
  workerKind: "general" | "coding" | "research" | "solo";
  conversationId?: string;
  imChannel?: ImChannel;
  imChatId?: string;
  createdAt: string;
  updatedAt: string;
  nextRunAt?: string;
  lastRunAt?: string;
}

export interface ScheduledTaskExecution {
  id: string;
  taskId: string;
  status: "running" | "completed" | "failed";
  result?: string;
  error?: string;
  startedAt: string;
  completedAt?: string;
  conversationId?: string;
}
