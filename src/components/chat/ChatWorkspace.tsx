import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { convertFileSrc } from "../../lib/electron-bridge";
import {
  ChevronDown,
  CircleAlert,
  FileText,
  Image as ImageIcon,
  LoaderCircle,
  Mic,
  MonitorSmartphone,
  PanelLeftOpen,
  Paperclip,
  Play,
  SendHorizonal,
  ShieldAlert,
  ShieldCheck,
  SquareStop,
  Square,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { executionStateLabel } from "../../lib/runLabels";
import type {
  AgentExecutionTrace,
  AssistantMessageBlock,
  AttachmentRef,
  AppSettings,
  ChatMessage,
  PermissionMode,
  SoloConfirmationPayload,
  SoloStatusPayload,
  ToolConfirmationPayload,
} from "../../types/protocol";

interface ChatWorkspaceProps {
  messages: ChatMessage[];
  canSend: boolean;
  onSend: (content: string, attachments?: AttachmentRef[]) => void;
  onTranscribeAudio: (params: {
    audioBase64: string;
    mimeType: string;
    durationMs: number;
  }) => Promise<string>;
  onSoloPause: () => boolean;
  onSoloResume: () => boolean;
  onSoloStop: () => boolean;
  onAllowDangerousStep: () => boolean;
  onRejectDangerousStep: () => boolean;
  onAllowToolConfirmation: () => boolean;
  onRejectToolConfirmation: () => boolean;
  onPermissionModeChange: (mode: PermissionMode) => void;
  onOpenMobileSidebar: () => void;
  settings: AppSettings;
  soloStatus: SoloStatusPayload;
  soloConfirmation: SoloConfirmationPayload | null;
  toolConfirmation: ToolConfirmationPayload | null;
  soloLastError: string | null;
}

type SlashItem = {
  id: string;
  category: "Tool" | "MCP" | "Skill";
  label: string;
  sublabel: string;
  value: string;
  enabled: boolean;
  keywords: string[];
};

type VoiceRecordingState = "idle" | "recording" | "transcribing";

const MIN_VOICE_DURATION_MS = 1_000;
const MIN_ACTIVE_VOICE_MS = 300;
const MAX_VOICE_AUDIO_BYTES = 7 * 1024 * 1024;
const VOICE_SAMPLE_INTERVAL_MS = 100;
const VOICE_ACTIVITY_RMS_THRESHOLD = 0.015;

function preferredVoiceMimeType() {
  const candidates = ["audio/webm;codecs=opus", "audio/webm"];
  return candidates.find((mimeType) => MediaRecorder.isTypeSupported(mimeType));
}

async function blobToBase64(blob: Blob) {
  const buffer = await blob.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function formatVoiceDuration(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function buildSlashItems(settings: AppSettings): SlashItem[] {
  return [
    ...settings.tools.map((item) => ({
      id: `tool-${item.id}`,
      category: "Tool" as const,
      label: item.name,
      sublabel: item.command || item.description || "未配置说明",
      value: `/tool ${item.name} `,
      enabled: item.enabled,
      keywords: [item.name, item.command, item.description].filter(Boolean),
    })),
    ...settings.mcp.map((item) => ({
      id: `mcp-${item.id}`,
      category: "MCP" as const,
      label: item.name,
      sublabel: item.endpoint || item.description || "未配置端点",
      value: `/mcp ${item.name} `,
      enabled: item.enabled,
      keywords: [item.name, item.endpoint, item.description].filter(Boolean),
    })),
    ...settings.skills.map((item) => ({
      id: `skill-${item.id}`,
      category: "Skill" as const,
      label: item.name,
      sublabel: item.description || item.prompt || "未配置说明",
      value: `/skill ${item.name} `,
      enabled: item.enabled,
      keywords: [item.name, item.description, item.prompt].filter(Boolean),
    })),
  ];
}

function findSlashQuery(draft: string, caretIndex: number) {
  const textBeforeCaret = draft.slice(0, caretIndex);
  const slashIndex = textBeforeCaret.lastIndexOf("/");
  if (slashIndex === -1) {
    return null;
  }

  const prefixChar = slashIndex === 0 ? "" : textBeforeCaret[slashIndex - 1];
  if (prefixChar && !/\s/.test(prefixChar)) {
    return null;
  }

  const queryText = textBeforeCaret.slice(slashIndex + 1);
  if (queryText.includes("\n")) {
    return null;
  }

  return { slashIndex, caretIndex, queryText };
}

function isNearBottom(element: HTMLDivElement, threshold = 40) {
  const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
  return remaining <= threshold;
}

const MAX_ATTACHMENTS_PER_MESSAGE = 5;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;

function attachmentKind(file: File): AttachmentRef["kind"] {
  if (file.type.startsWith("image/")) {
    return "image";
  }
  if (file.type.startsWith("audio/")) {
    return "audio";
  }
  if (file.type.startsWith("video/")) {
    return "video";
  }
  return "file";
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("failed to read file"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("file reader did not return data URL"));
        return;
      }
      resolve(result);
    };
    reader.readAsDataURL(file);
  });
}

function formatBytes(size: number) {
  if (!Number.isFinite(size) || size <= 0) {
    return "未知大小";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function attachmentImageSrc(attachment: AttachmentRef) {
  if (attachment.previewUrl) {
    return attachment.previewUrl;
  }
  if (attachment.contentBase64?.startsWith("data:")) {
    return attachment.contentBase64;
  }
  if (!attachment.localPath) {
    return undefined;
  }
  return convertFileSrc(attachment.localPath.replace(/\//g, "\\"));
}

function publicAttachments(attachments: AttachmentRef[]) {
  return attachments.map(({ previewUrl, ...attachment }) => attachment);
}

function createAttachmentId() {
  return `att-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function formatTraceValue(value: unknown) {
  if (typeof value === "string") {
    return value;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatDurationMs(duration: number) {
  if (duration < 1000) {
    return `${duration}ms`;
  }
  return `${(duration / 1000).toFixed(duration >= 10_000 ? 0 : 1)}s`;
}

function formatTraceDuration(startedAt: string, completedAt?: string) {
  if (!completedAt) {
    return "进行中";
  }

  const duration = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(duration) || duration < 0) {
    return "刚刚";
  }
  return formatDurationMs(duration);
}

function renderMessageMarkdown(content: string) {
  return (
    <div className="message-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} rel="noopener noreferrer nofollow" target="_blank" />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function AttachmentList({
  attachments,
  removable = false,
  onRemove,
}: {
  attachments?: AttachmentRef[];
  removable?: boolean;
  onRemove?: (id: string) => void;
}) {
  if (!attachments?.length) {
    return null;
  }

  return (
    <div className="attachment-list">
      {attachments.map((attachment) => {
        const imgSrc = attachment.kind === "image" ? attachmentImageSrc(attachment) : undefined;
        return (
          <div key={attachment.id} className={`attachment-chip status-${attachment.status ?? "ready"}`}>
            {imgSrc ? (
              <img alt={attachment.name || "attachment"} src={imgSrc} />
            ) : attachment.kind === "image" ? (
              <ImageIcon size={16} />
            ) : (
              <FileText size={16} />
            )}
            <div className="attachment-copy">
              <strong>{attachment.name || "attachment"}</strong>
              <span>
                {attachment.error
                  ? attachment.error
                  : `${attachment.kind} · ${formatBytes(attachment.size)}`}
              </span>
            </div>
            {attachment.localPath ? (
              <button
                className="attachment-open"
                onClick={() => window.open(convertFileSrc(attachment.localPath!.replace(/\//g, "\\")))}
                type="button"
              >
                打开
              </button>
            ) : null}
            {removable ? (
              <button
                aria-label="移除附件"
                className="attachment-remove"
                onClick={() => onRemove?.(attachment.id)}
                type="button"
              >
                <X size={14} />
              </button>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function traceKeyFromMessage(message: ChatMessage, trace: AgentExecutionTrace) {
  return `${message.id}:${trace.id}`;
}

function timestampMs(value?: string) {
  if (!value) {
    return undefined;
  }
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : undefined;
}

function isInternalMemoryTrace(trace: AgentExecutionTrace) {
  return trace.name.trim().toLowerCase().startsWith("memory.");
}

function collectUniqueMessageTraces(message: ChatMessage) {
  const traceMap = new Map<string, AgentExecutionTrace>();
  for (const trace of message.traces ?? []) {
    traceMap.set(trace.id, trace);
  }
  for (const block of message.blocks ?? []) {
    if (block.kind !== "trace") {
      continue;
    }
    const existing = traceMap.get(block.trace.id);
    traceMap.set(block.trace.id, existing ? { ...block.trace, ...existing } : block.trace);
  }
  return Array.from(traceMap.values()).filter((trace) => !isInternalMemoryTrace(trace));
}

function isInternalMemoryMessage(message: ChatMessage) {
  const allTraces = [
    ...(message.traces ?? []),
    ...(message.blocks ?? [])
      .filter((block): block is Extract<AssistantMessageBlock, { kind: "trace" }> => block.kind === "trace")
      .map((block) => block.trace),
  ];
  return (
    allTraces.length > 0 &&
    allTraces.every(isInternalMemoryTrace) &&
    (message.role === "tool" || !message.content.trim())
  );
}

function isSoloHandoffTrace(trace: AgentExecutionTrace) {
  const workerKind = trace.params?.workerKind;
  const route = trace.params?.route;
  return (
    trace.name.toLowerCase().includes("solo-worker") ||
    workerKind === "solo" ||
    route === "start_solo"
  );
}

function formatMessageDuration(message: ChatMessage, traces: AgentExecutionTrace[]) {
  const starts = [
    timestampMs(message.createdAt),
    ...traces.map((trace) => timestampMs(trace.startedAt)),
  ].filter((value): value is number => value !== undefined);
  const ends = [
    timestampMs(message.completedAt),
    ...traces.map((trace) => timestampMs(trace.completedAt)),
  ].filter((value): value is number => value !== undefined);

  if (starts.length === 0 || ends.length === 0) {
    return "已完成";
  }

  const startedAt = Math.min(...starts);
  const completedAt = Math.max(...ends);
  if (completedAt < startedAt) {
    return "已完成";
  }
  return `用时 ${formatDurationMs(completedAt - startedAt)}`;
}

function formatTokenCount(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

type RenderedAssistantBlock =
  | AssistantMessageBlock
  | {
      id: string;
      kind: "trace-group";
      traces: AgentExecutionTrace[];
    };

function compactTraceTitle(trace: AgentExecutionTrace) {
  const command = trace.params?.command;
  if (typeof command === "string" && command.trim()) {
    return command.trim();
  }
  return trace.name;
}

function traceGroupStatus(traces: AgentExecutionTrace[]) {
  const completed = traces.filter((trace) => trace.status === "completed").length;
  const failed = traces.filter((trace) => trace.status === "error").length;
  const running = traces.filter((trace) => trace.status === "started").length;
  const total = traces.length;
  const label = running
    ? `正在执行 ${total} 个工具调用`
    : failed
      ? `已完成 ${total} 个工具调用，${failed} 个失败`
      : `已完成 ${total} 个工具调用`;

  return { completed, failed, running, total, label };
}

function traceStatusClass(trace: AgentExecutionTrace) {
  if (trace.status === "completed") {
    return "is-completed";
  }
  if (trace.status === "error") {
    return "has-error";
  }
  if (trace.status === "started") {
    return "is-running";
  }
  return "";
}

function messageHasTrace(message: ChatMessage) {
  return Boolean(
    (message.traces && message.traces.length > 0) ||
      message.blocks?.some((block) => block.kind === "trace"),
  );
}

function shouldShowMessageLabel(message: ChatMessage) {
  if (!message.label) {
    return false;
  }
  return !(message.role === "tool" && messageHasTrace(message));
}

function groupAssistantBlocks(blocks: AssistantMessageBlock[]) {
  const grouped: RenderedAssistantBlock[] = [];
  let traceBuffer: AgentExecutionTrace[] = [];

  const flushTraceBuffer = () => {
    if (traceBuffer.length === 0) {
      return;
    }
    const first = traceBuffer[0];
    const last = traceBuffer[traceBuffer.length - 1];
    grouped.push({
      id: `trace-group-${first.id}-${last.id}`,
      kind: "trace-group",
      traces: traceBuffer,
    });
    traceBuffer = [];
  };

  for (const block of blocks) {
    if (block.kind === "trace") {
      traceBuffer.push(block.trace);
      continue;
    }
    flushTraceBuffer();
    grouped.push(block);
  }

  flushTraceBuffer();
  return grouped;
}

type MessageListItem =
  | {
      id: string;
      kind: "message";
      message: ChatMessage;
      soloProcessMessages?: ChatMessage[];
    }
  | {
      id: string;
      kind: "solo-process-group";
      messages: ChatMessage[];
    }
  | {
      id: string;
      kind: "tool-group";
      messages: ChatMessage[];
    };

function isSoloAssistantProcessMessage(message: ChatMessage) {
  return message.role === "assistant" && message.mode === "solo" && Boolean(message.content.trim());
}

function canAttachSoloProcessMessages(message: ChatMessage) {
  return (
    message.role === "assistant" &&
    Boolean(message.requestId) &&
    collectUniqueMessageTraces(message).some(isSoloHandoffTrace)
  );
}

function buildMessageListItems(messages: ChatMessage[], collapseProcess: boolean) {
  const items: MessageListItem[] = [];
  let toolBuffer: ChatMessage[] = [];
  let soloAssistantBuffer: ChatMessage[] = [];

  const flushToolBuffer = () => {
    if (toolBuffer.length === 0) {
      return;
    }
    items.push({
      id: `tool-group-${toolBuffer[0].id}-${toolBuffer[toolBuffer.length - 1].id}`,
      kind: "tool-group",
      messages: toolBuffer,
    });
    toolBuffer = [];
  };

  const attachSoloProcessMessages = (processMessages: ChatMessage[]) => {
    if (processMessages.length === 0) {
      return false;
    }
    const requestId = processMessages[0].requestId;
    if (!requestId) {
      return false;
    }
    let targetIndex = -1;
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (
        item.kind === "message" &&
        item.message.requestId === requestId &&
        canAttachSoloProcessMessages(item.message)
      ) {
        targetIndex = index;
        break;
      }
    }
    if (targetIndex < 0) {
      return false;
    }
    const target = items[targetIndex];
    if (target.kind !== "message") {
      return false;
    }
    items[targetIndex] = {
      ...target,
      soloProcessMessages: [...(target.soloProcessMessages ?? []), ...processMessages],
    };
    return true;
  };

  const flushSoloAssistantBuffer = () => {
    if (soloAssistantBuffer.length === 0) {
      return;
    }
    if (soloAssistantBuffer.length === 1) {
      items.push({
        id: soloAssistantBuffer[0].id,
        kind: "message",
        message: soloAssistantBuffer[0],
      });
      soloAssistantBuffer = [];
      return;
    }

    const processMessages = soloAssistantBuffer.slice(0, -1);
    const latestMessage = soloAssistantBuffer[soloAssistantBuffer.length - 1];
    if (!attachSoloProcessMessages(processMessages)) {
      items.push({
        id: `solo-process-group-${processMessages[0].id}-${processMessages[processMessages.length - 1].id}`,
        kind: "solo-process-group",
        messages: processMessages,
      });
    }
    items.push({
      id: latestMessage.id,
      kind: "message",
      message: latestMessage,
    });
    soloAssistantBuffer = [];
  };

  for (const message of messages) {
    if (!collapseProcess) {
      items.push({
        id: message.id,
        kind: "message",
        message,
      });
      continue;
    }

    if (message.role === "tool" && message.traces && message.traces.length > 0) {
      flushSoloAssistantBuffer();
      toolBuffer.push(message);
      continue;
    }
    flushToolBuffer();

    if (isSoloAssistantProcessMessage(message)) {
      const bufferRequestId = soloAssistantBuffer[0]?.requestId;
      if (soloAssistantBuffer.length > 0 && bufferRequestId !== message.requestId) {
        flushSoloAssistantBuffer();
      }
      soloAssistantBuffer.push(message);
      continue;
    }

    flushSoloAssistantBuffer();
    items.push({
      id: message.id,
      kind: "message",
      message,
    });
  }

  flushToolBuffer();
  flushSoloAssistantBuffer();
  return items;
}

type TraceTimelineItem = {
  key: string;
  trace: AgentExecutionTrace;
};

function TraceTimeline(props: {
  items: TraceTimelineItem[];
  expandedTraceIds: Set<string>;
  onToggleTrace: (traceKey: string) => void;
}) {
  const { items, expandedTraceIds, onToggleTrace } = props;
  if (items.length === 0) {
    return null;
  }

  return (
    <div className="trace-timeline">
      {items.map((item) => {
        const { trace } = item;
        const isExpanded = expandedTraceIds.has(item.key);

        return (
          <div
            key={item.key}
            className={`trace-step ${traceStatusClass(trace)} ${isExpanded ? "is-expanded" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              onToggleTrace(item.key);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onToggleTrace(item.key);
              }
            }}
            role="button"
            tabIndex={0}
          >
            <div className="trace-step-header">
              <span className="trace-step-title">{compactTraceTitle(trace)}</span>
              <small className="trace-step-duration">
                {formatTraceDuration(trace.startedAt, trace.completedAt)}
              </small>
              <ChevronDown
                size={12}
                className="trace-step-chevron"
                style={{ transform: isExpanded ? "rotate(180deg)" : "none" }}
              />
            </div>
            {isExpanded && (trace.result || trace.params) ? (
              <div className="trace-step-detail">
                <div className="trace-step-detail-head">
                  <span>{trace.name}</span>
                  <span>{trace.status === "completed" ? "成功" : trace.status}</span>
                </div>
                {trace.result ? <pre>{formatTraceValue(trace.result)}</pre> : null}
                {!trace.result && trace.params ? <pre>{formatTraceValue(trace.params)}</pre> : null}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function TraceGroup(props: {
  message: ChatMessage;
  group: Extract<RenderedAssistantBlock, { kind: "trace-group" }>;
  expandedTraceIds: Set<string>;
  onToggleTrace: (traceKey: string) => void;
}) {
  const { message, group, expandedTraceIds, onToggleTrace } = props;
  const [groupCollapsed, setGroupCollapsed] = useState(group.traces.length > 3);
  const hasError = group.traces.some((t) => t.status === "error");
  const isRunning = group.traces.some((t) => t.status === "started");
  const status = traceGroupStatus(group.traces);
  const traceItems = group.traces.map((trace) => ({
    key: traceKeyFromMessage(message, trace),
    trace,
  }));

  if (group.traces.length > 1) {
    return (
      <div className="trace-group-wrapper">
        <div
          className={`trace-group-header ${hasError ? "has-error" : ""} ${isRunning ? "is-running" : ""}`}
          onClick={() => setGroupCollapsed((c) => !c)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setGroupCollapsed((c) => !c);
            }
          }}
          role="button"
          tabIndex={0}
        >
          <span
            className={`tool-summary-dot ${hasError ? "has-error" : isRunning ? "is-running" : "is-completed"}`}
          />
          <span className="tool-summary-copy">
            <span className="trace-group-label">{status.label}</span>
            <span className="tool-summary-meta">
              {status.completed}/{status.total} 完成
              {status.failed ? ` · ${status.failed} 失败` : ""}
            </span>
          </span>
          <ChevronDown
            size={13}
            className="trace-group-chevron"
            style={{ transform: groupCollapsed ? "none" : "rotate(180deg)" }}
          />
        </div>
        {!groupCollapsed ? (
          <TraceTimeline
            expandedTraceIds={expandedTraceIds}
            items={traceItems}
            onToggleTrace={onToggleTrace}
          />
        ) : null}
      </div>
    );
  }

  return (
    <TraceTimeline
      expandedTraceIds={expandedTraceIds}
      items={traceItems}
      onToggleTrace={onToggleTrace}
    />
  );
}

function ToolMessageGroup(props: {
  messages: ChatMessage[];
  expandedTraceIds: Set<string>;
  onToggleTrace: (traceKey: string) => void;
  isCollapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  const { messages, expandedTraceIds, onToggleTrace, isCollapsed, onToggleCollapsed } = props;

  const traceItems = messages.flatMap((message) =>
    (message.traces ?? []).map((trace) => ({
      key: traceKeyFromMessage(message, trace),
      trace,
    })),
  );
  const allTraces = traceItems.map((item) => item.trace);
  const hasError = allTraces.some((t) => t.status === "error");
  const isRunning = allTraces.some((t) => t.status === "started");
  const count = allTraces.length;
  const status = traceGroupStatus(allTraces);

  return (
    <article className="message-shell role-tool tool-message-group">
      <div
        className={`tool-group-header ${hasError ? "has-error" : ""} ${isRunning ? "is-running" : ""}`}
        onClick={onToggleCollapsed}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggleCollapsed();
          }
        }}
        role="button"
        tabIndex={0}
      >
        <span
          className={`tool-summary-dot ${hasError ? "has-error" : isRunning ? "is-running" : "is-completed"}`}
        />
        <span className="tool-summary-copy">
          <span className="tool-group-label">{status.label}</span>
          <span className="tool-summary-meta">
            {status.completed}/{count} 完成
            {status.failed ? ` · ${status.failed} 失败` : ""}
          </span>
        </span>
        <ChevronDown
          size={13}
          className="tool-group-chevron"
          style={{ transform: isCollapsed ? "none" : "rotate(180deg)" }}
        />
      </div>
      {!isCollapsed ? (
        <TraceTimeline
          expandedTraceIds={expandedTraceIds}
          items={traceItems}
          onToggleTrace={onToggleTrace}
        />
      ) : null}
    </article>
  );
}

function formatSoloProcessDuration(messages: ChatMessage[]) {
  const starts = messages.map((message) => timestampMs(message.createdAt)).filter((value): value is number => value !== undefined);
  const ends = messages
    .map((message) => timestampMs(message.completedAt ?? message.createdAt))
    .filter((value): value is number => value !== undefined);
  if (starts.length === 0 || ends.length === 0) {
    return "已折叠";
  }
  const startedAt = Math.min(...starts);
  const completedAt = Math.max(...ends);
  if (completedAt < startedAt) {
    return "已折叠";
  }
  return `用时 ${formatDurationMs(completedAt - startedAt)}`;
}

function SoloProcessMessageGroup(props: {
  messages: ChatMessage[];
  isCollapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  const { messages, isCollapsed, onToggleCollapsed } = props;
  const hasError = messages.some((message) => message.status === "error");
  const label = `已折叠 ${messages.length} 条执行过程`;

  return (
    <article className="message-shell role-assistant mode-solo solo-process-group">
      <div
        aria-expanded={!isCollapsed}
        className={`assistant-process-summary ${hasError ? "has-error" : ""}`}
        onClick={onToggleCollapsed}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggleCollapsed();
          }
        }}
        role="button"
        tabIndex={0}
      >
        <span className={`tool-summary-dot ${hasError ? "has-error" : "is-completed"}`} />
        <span className="tool-summary-copy">
          <span className="assistant-process-label">{label}</span>
          <span className="tool-summary-meta">{formatSoloProcessDuration(messages)}</span>
        </span>
        <ChevronDown
          size={13}
          className="trace-group-chevron"
          style={{ transform: isCollapsed ? "none" : "rotate(180deg)" }}
        />
      </div>
      {!isCollapsed ? (
        <div className="assistant-process-detail">
          {messages.map((message) => (
            <div key={message.id} className="assistant-process-progress">
              {renderMessageMarkdown(message.content)}
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

type AssistantTextBlockItem = Extract<AssistantMessageBlock, { kind: "text" }>;

type AssistantProcessTimelineItem =
  | {
      id: string;
      kind: "text";
      content: string;
      timestamp?: number;
      order: number;
    }
  | {
      id: string;
      kind: "trace";
      trace: AgentExecutionTrace;
      timestamp?: number;
      order: number;
    };

function buildAssistantProcessTimeline(params: {
  message: ChatMessage;
  progressBlocks: AssistantTextBlockItem[];
  soloProcessMessages: ChatMessage[];
  traces: AgentExecutionTrace[];
}) {
  const { message, progressBlocks, soloProcessMessages, traces } = params;
  const timeline: AssistantProcessTimelineItem[] = [];
  const includedTextIds = new Set(progressBlocks.map((block) => block.id));
  const traceMap = new Map(traces.map((trace) => [trace.id, trace]));
  const includedTraceIds = new Set<string>();
  let order = 0;

  for (const block of message.blocks ?? []) {
    if (block.kind === "text") {
      if (includedTextIds.has(block.id) && block.content) {
        timeline.push({
          id: block.id,
          kind: "text",
          content: block.content,
          timestamp: timestampMs(message.createdAt),
          order,
        });
        order += 1;
      }
      continue;
    }

    const trace = traceMap.get(block.trace.id) ?? block.trace;
    includedTraceIds.add(trace.id);
    timeline.push({
      id: `trace-${trace.id}`,
      kind: "trace",
      trace,
      timestamp: timestampMs(trace.startedAt),
      order,
    });
    order += 1;
  }

  for (const block of progressBlocks) {
    if (!includedTextIds.has(block.id) || !block.content) {
      continue;
    }
    if (timeline.some((item) => item.id === block.id)) {
      continue;
    }
    timeline.push({
      id: block.id,
      kind: "text",
      content: block.content,
      timestamp: timestampMs(message.createdAt),
      order,
    });
    order += 1;
  }

  for (const processMessage of soloProcessMessages) {
    timeline.push({
      id: processMessage.id,
      kind: "text",
      content: processMessage.content,
      timestamp: timestampMs(processMessage.createdAt),
      order,
    });
    order += 1;
  }

  for (const trace of traces) {
    if (includedTraceIds.has(trace.id)) {
      continue;
    }
    timeline.push({
      id: `trace-${trace.id}`,
      kind: "trace",
      trace,
      timestamp: timestampMs(trace.startedAt),
      order,
    });
    order += 1;
  }

  return timeline.sort((left, right) => {
    if (left.timestamp !== undefined && right.timestamp !== undefined && left.timestamp !== right.timestamp) {
      return left.timestamp - right.timestamp;
    }
    if (left.timestamp !== undefined && right.timestamp === undefined) {
      return -1;
    }
    if (left.timestamp === undefined && right.timestamp !== undefined) {
      return 1;
    }
    return left.order - right.order;
  });
}

function AssistantProcessSummary(props: {
  message: ChatMessage;
  progressBlocks: AssistantTextBlockItem[];
  soloProcessMessages: ChatMessage[];
  traces: AgentExecutionTrace[];
  expandedTraceIds: Set<string>;
  onToggleTrace: (traceKey: string) => void;
  isCollapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  const {
    message,
    progressBlocks,
    soloProcessMessages,
    traces,
    expandedTraceIds,
    onToggleTrace,
    isCollapsed,
    onToggleCollapsed,
  } = props;
  const hasError = traces.some((trace) => trace.status === "error");
  const isRunning = traces.some((trace) => trace.status === "started");
  const durationLabel = formatMessageDuration(message, traces);
  const timelineItems = buildAssistantProcessTimeline({
    message,
    progressBlocks,
    soloProcessMessages,
    traces,
  });

  return (
    <div className="assistant-process">
      <div
        aria-expanded={!isCollapsed}
        className={`assistant-process-summary ${hasError ? "has-error" : ""} ${isRunning ? "is-running" : ""}`}
        onClick={onToggleCollapsed}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggleCollapsed();
          }
        }}
        role="button"
        tabIndex={0}
      >
        <span
          className={`tool-summary-dot ${hasError ? "has-error" : isRunning ? "is-running" : "is-completed"}`}
        />
        <span className="tool-summary-copy">
          <span className="assistant-process-label">{durationLabel}</span>
        </span>
        <ChevronDown
          size={13}
          className="trace-group-chevron"
          style={{ transform: isCollapsed ? "none" : "rotate(180deg)" }}
        />
      </div>
      {!isCollapsed ? (
        <div className="assistant-process-detail">
          {timelineItems.map((item) =>
            item.kind === "text" ? (
              <div key={item.id} className="assistant-process-progress">
                {renderMessageMarkdown(item.content)}
              </div>
            ) : (
              <TraceTimeline
                key={item.id}
                expandedTraceIds={expandedTraceIds}
                items={[{ key: traceKeyFromMessage(message, item.trace), trace: item.trace }]}
                onToggleTrace={onToggleTrace}
              />
            ),
          )}
        </div>
      ) : null}
    </div>
  );
}

function LiveAssistantContent(props: {
  message: ChatMessage;
  expandedTraceIds: Set<string>;
  onToggleTrace: (traceKey: string) => void;
}) {
  const { message, expandedTraceIds, onToggleTrace } = props;
  const blocks = (message.blocks ?? []).filter(
    (block) => block.kind !== "trace" || !isInternalMemoryTrace(block.trace),
  );

  if (blocks.length > 0) {
    return (
      <div className="assistant-blocks">
        {groupAssistantBlocks(blocks).map((block) =>
          block.kind === "text" ? (
            block.content ? (
              <div
                key={block.id}
                className={`assistant-text-panel ${block.purpose === "progress" ? "is-progress" : ""}`}
              >
                {renderMessageMarkdown(block.content)}
              </div>
            ) : null
          ) : block.kind === "trace-group" ? (
            <TraceTimeline
              key={block.id}
              expandedTraceIds={expandedTraceIds}
              items={block.traces.map((trace) => ({
                key: traceKeyFromMessage(message, trace),
                trace,
              }))}
              onToggleTrace={onToggleTrace}
            />
          ) : (
            <TraceTimeline
              key={block.id}
              expandedTraceIds={expandedTraceIds}
              items={[{ key: traceKeyFromMessage(message, block.trace), trace: block.trace }]}
              onToggleTrace={onToggleTrace}
            />
          ),
        )}
        {message.content && !blocks.some((block) => block.kind === "text" && block.content) ? (
          <div className="assistant-text-panel">{renderMessageMarkdown(message.content)}</div>
        ) : null}
      </div>
    );
  }

  return (
    <>
      {message.content ? (
        <div className="assistant-text-panel">{renderMessageMarkdown(message.content)}</div>
      ) : null}
      {collectUniqueMessageTraces(message).length > 0 ? (
        <TraceTimeline
          expandedTraceIds={expandedTraceIds}
          items={collectUniqueMessageTraces(message).map((trace) => ({
            key: traceKeyFromMessage(message, trace),
            trace,
          }))}
          onToggleTrace={onToggleTrace}
        />
      ) : null}
    </>
  );
}

function AssistantMessageContent(props: {
  message: ChatMessage;
  soloProcessMessages: ChatMessage[];
  expandedTraceIds: Set<string>;
  onToggleTrace: (traceKey: string) => void;
  isProcessCollapsed: boolean;
  onToggleProcessCollapsed: () => void;
  collapseProcess: boolean;
}) {
  const {
    message,
    soloProcessMessages,
    expandedTraceIds,
    onToggleTrace,
    isProcessCollapsed,
    onToggleProcessCollapsed,
    collapseProcess,
  } = props;
  const blocks = (message.blocks ?? []).filter(
    (block) => block.kind !== "trace" || !isInternalMemoryTrace(block.trace),
  );
  const traces = collectUniqueMessageTraces(message);
  const isSoloHandoff = traces.some(isSoloHandoffTrace);

  if (message.status === "pending" || (!collapseProcess && (isSoloHandoff || message.mode === "solo"))) {
    return (
      <LiveAssistantContent
        expandedTraceIds={expandedTraceIds}
        message={message}
        onToggleTrace={onToggleTrace}
      />
    );
  }

  const textBlocks = blocks.filter(
    (block): block is AssistantTextBlockItem => block.kind === "text",
  );
  const progressBlocks = textBlocks.filter((block) => block.purpose === "progress");
  const finalBlocks = textBlocks.filter((block) => block.purpose !== "progress");
  const processBlocks = isSoloHandoff
    ? textBlocks.filter((block) => Boolean(block.content))
    : progressBlocks;
  const visibleFinalBlocks = isSoloHandoff ? [] : finalBlocks;
  const shouldShowProcess =
    processBlocks.length > 0 || soloProcessMessages.length > 0 || traces.length > 0;
  const fallbackFinalContent =
    !isSoloHandoff && finalBlocks.length === 0 && message.content && progressBlocks.length === 0
      ? message.content
      : "";

  return (
    <div className="assistant-blocks is-complete">
      {shouldShowProcess ? (
        <AssistantProcessSummary
          expandedTraceIds={expandedTraceIds}
          isCollapsed={isProcessCollapsed}
          message={message}
          onToggleCollapsed={onToggleProcessCollapsed}
          onToggleTrace={onToggleTrace}
          progressBlocks={processBlocks}
          soloProcessMessages={soloProcessMessages}
          traces={traces}
        />
      ) : null}

      {visibleFinalBlocks.map((block) =>
        block.content ? (
          <div key={block.id} className="assistant-final-card">
            {renderMessageMarkdown(block.content)}
          </div>
        ) : null,
      )}

      {fallbackFinalContent ? (
        <div className="assistant-final-card">{renderMessageMarkdown(fallbackFinalContent)}</div>
      ) : null}
    </div>
  );
}

const MessageArticle = memo(function MessageArticle({
  message,
  soloProcessMessages,
  expandedTraceIds,
  onToggleTrace,
  isProcessCollapsed,
  onToggleProcessCollapsed,
  collapseProcess,
}: {
  message: ChatMessage;
  soloProcessMessages: ChatMessage[];
  expandedTraceIds: Set<string>;
  onToggleTrace: (traceKey: string) => void;
  isProcessCollapsed: boolean;
  onToggleProcessCollapsed: () => void;
  collapseProcess: boolean;
}) {
  return (
    <article
      className={`message-shell role-${message.role} ${message.mode === "solo" ? "mode-solo" : ""}`}
    >
      <div className="message-meta">
        <strong>
          {message.role === "user"
            ? "你"
            : message.role === "assistant"
              ? "Agent"
              : message.role === "tool"
                ? "工具"
                : "系统"}
        </strong>
        <span>{new Date(message.createdAt).toLocaleTimeString()}</span>
      </div>

      {shouldShowMessageLabel(message) ? <div className="message-label">{message.label}</div> : null}

      {message.role === "assistant" ? (
        <AssistantMessageContent
          expandedTraceIds={expandedTraceIds}
          isProcessCollapsed={isProcessCollapsed}
          message={message}
          onToggleProcessCollapsed={onToggleProcessCollapsed}
          onToggleTrace={onToggleTrace}
          soloProcessMessages={soloProcessMessages}
          collapseProcess={collapseProcess}
        />
      ) : message.blocks && message.blocks.length > 0 ? (
        <div className="assistant-blocks">
          {groupAssistantBlocks(message.blocks).map((block) =>
            block.kind === "text" ? (
              block.content ? (
                <div key={block.id} className="assistant-text-panel">
                  {renderMessageMarkdown(block.content)}
                </div>
              ) : null
            ) : block.kind === "trace-group" ? (
              <TraceGroup
                key={block.id}
                expandedTraceIds={expandedTraceIds}
                group={block}
                message={message}
                onToggleTrace={onToggleTrace}
              />
            ) : (
              <TraceGroup
                key={block.id}
                expandedTraceIds={expandedTraceIds}
                group={{ id: `trace-group-${block.trace.id}`, kind: "trace-group", traces: [block.trace] }}
                message={message}
                onToggleTrace={onToggleTrace}
              />
            ),
          )}
          {/* blocks 全是 trace 没有正文时，用 content 兜底 */}
          {message.content && !message.blocks.some((b) => b.kind === "text" && b.content) ? (
            <div className="assistant-text-panel">{renderMessageMarkdown(message.content)}</div>
          ) : null}
        </div>
      ) : message.content ? (
        renderMessageMarkdown(message.content)
      ) : null}

      <AttachmentList attachments={message.attachments} />

      {message.role !== "assistant" &&
      (!message.blocks || message.blocks.length === 0) &&
      message.traces &&
      message.traces.length > 0 ? (
        <TraceGroup
          expandedTraceIds={expandedTraceIds}
          group={{
            id: `trace-group-${message.traces[0]?.id ?? message.id}`,
            kind: "trace-group",
            traces: message.traces,
          }}
          message={message}
          onToggleTrace={onToggleTrace}
        />
      ) : null}

      {message.role === "assistant" && message.status === "pending" ? (
        <div className="message-thinking" aria-label="AI 正在思考">
          <span />
          <span />
          <span />
        </div>
      ) : null}

      {message.role === "assistant" && message.tokenUsage?.totalTokens ? (
        <div
          className="message-token-usage"
          title={`${message.tokenUsage.calls} 次模型调用`}
        >
          本任务消耗 {formatTokenCount(message.tokenUsage.totalTokens)} tokens · 输入{" "}
          {formatTokenCount(message.tokenUsage.inputTokens)} · 输出{" "}
          {formatTokenCount(message.tokenUsage.outputTokens)}
        </div>
      ) : null}
    </article>
  );
});

function ChatWorkspaceComponent(props: ChatWorkspaceProps) {
  const {
    messages,
    canSend,
    onSend,
    onTranscribeAudio,
    onSoloPause,
    onSoloResume,
    onSoloStop,
    onAllowDangerousStep,
    onRejectDangerousStep,
    onAllowToolConfirmation,
    onRejectToolConfirmation,
    onPermissionModeChange,
    onOpenMobileSidebar,
    settings,
    soloStatus,
    soloConfirmation,
    toolConfirmation,
    soloLastError,
  } = props;
  const [draft, setDraft] = useState("");
  const [draftAttachments, setDraftAttachments] = useState<AttachmentRef[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [voiceState, setVoiceState] = useState<VoiceRecordingState>("idle");
  const [voiceElapsedSeconds, setVoiceElapsedSeconds] = useState(0);
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [expandedTraceIds, setExpandedTraceIds] = useState<Set<string>>(new Set());
  const [expandedProcessGroups, setExpandedProcessGroups] = useState<Set<string>>(new Set());
  const draftRef = useRef(draft);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const voiceSampleTimerRef = useRef<number | null>(null);
  const voiceDisplayTimerRef = useRef<number | null>(null);
  const voiceTimeoutRef = useRef<number | null>(null);
  const voiceChunksRef = useRef<Blob[]>([]);
  const voiceStartedAtRef = useRef(0);
  const voiceDurationMsRef = useRef(0);
  const activeVoiceMsRef = useRef(0);
  const voiceActivityDetectionAvailableRef = useRef(false);
  const voiceCancelledRef = useRef(false);

  draftRef.current = draft;

  const slashItems = useMemo(() => buildSlashItems(settings), [settings]);
  const caretIndex = textareaRef.current?.selectionStart ?? draft.length;
  const slashQuery = findSlashQuery(draft, caretIndex);
  const normalizedQuery = slashQuery?.queryText.trim().toLowerCase() ?? "";

  const filteredSlashItems = useMemo(() => {
    if (!slashQuery) {
      return [];
    }

    if (!normalizedQuery) {
      return slashItems;
    }

    return slashItems.filter((item) =>
      [item.category, item.label, item.sublabel, ...item.keywords].some((field) =>
        field.toLowerCase().includes(normalizedQuery),
      ),
    );
  }, [normalizedQuery, slashItems, slashQuery]);

  const groupedItems = useMemo(() => {
    const order: SlashItem["category"][] = ["Tool", "MCP", "Skill"];
    return order
      .map((category) => ({
        category,
        items: filteredSlashItems.filter((item) => item.category === category),
      }))
      .filter((group) => group.items.length > 0);
  }, [filteredSlashItems]);

  const flatItems = useMemo(() => groupedItems.flatMap((group) => group.items), [groupedItems]);
  const isSoloBusy =
    soloStatus.state === "running" ||
    soloStatus.state === "paused" ||
    soloStatus.state === "waiting_user_confirmation";
  const collapseProcess = !isSoloBusy;

  const visibleMessages = useMemo(
    () =>
      messages.filter(
        (message) =>
          !(message.mode === "solo" && (message.imagePath || message.role === "tool")) &&
          !isInternalMemoryMessage(message),
      ),
    [messages],
  );
  const messageItems = useMemo(
    () => buildMessageListItems(visibleMessages, collapseProcess),
    [collapseProcess, visibleMessages],
  );

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) {
      return;
    }
    element.style.height = "0px";
    const nextHeight = Math.min(Math.max(element.scrollHeight, 80), 170);
    element.style.height = `${nextHeight}px`;
  }, [draft]);

  const clearVoiceTimers = () => {
    if (voiceSampleTimerRef.current) {
      window.clearInterval(voiceSampleTimerRef.current);
      voiceSampleTimerRef.current = null;
    }
    if (voiceDisplayTimerRef.current) {
      window.clearInterval(voiceDisplayTimerRef.current);
      voiceDisplayTimerRef.current = null;
    }
    if (voiceTimeoutRef.current) {
      window.clearTimeout(voiceTimeoutRef.current);
      voiceTimeoutRef.current = null;
    }
  };

  const releaseVoiceResources = () => {
    clearVoiceTimers();
    const context = audioContextRef.current;
    audioContextRef.current = null;
    if (context) {
      void context.close().catch(() => {});
    }
    const stream = mediaStreamRef.current;
    mediaStreamRef.current = null;
    stream?.getTracks().forEach((track) => track.stop());
    mediaRecorderRef.current = null;
  };

  const insertVoiceTranscript = (text: string) => {
    const textarea = textareaRef.current;
    const currentDraft = draftRef.current;
    const start = textarea?.selectionStart ?? currentDraft.length;
    const end = textarea?.selectionEnd ?? start;
    const nextDraft = `${currentDraft.slice(0, start)}${text}${currentDraft.slice(end)}`;
    setDraft(nextDraft);
    setActiveIndex(0);
    requestAnimationFrame(() => {
      textarea?.focus();
      const caret = start + text.length;
      textarea?.setSelectionRange(caret, caret);
    });
  };

  const stopVoiceRecording = () => {
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state === "inactive") {
      return;
    }
    voiceDurationMsRef.current = Math.max(1, Date.now() - voiceStartedAtRef.current);
    clearVoiceTimers();
    setVoiceState("transcribing");
    recorder.stop();
  };

  const cancelVoiceRecording = () => {
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state === "inactive") {
      return;
    }
    voiceCancelledRef.current = true;
    clearVoiceTimers();
    setVoiceState("idle");
    recorder.stop();
  };

  const startVoiceRecording = async () => {
    if (
      !settings.voiceInput.enabled ||
      !settings.voiceInput.apiKey.trim() ||
      !settings.voiceInput.baseUrl.trim() ||
      !settings.voiceInput.modelId.trim()
    ) {
      setVoiceError("请先在设置 → 语音输入中完成配置。");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setVoiceError("当前环境不支持麦克风录音。");
      return;
    }

    setVoiceError(null);
    voiceCancelledRef.current = false;
    voiceChunksRef.current = [];
    activeVoiceMsRef.current = 0;
    voiceActivityDetectionAvailableRef.current = false;
    voiceDurationMsRef.current = 0;
    setVoiceElapsedSeconds(0);

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      const mimeType = preferredVoiceMimeType();
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      mediaStreamRef.current = stream;
      mediaRecorderRef.current = recorder;
      voiceStartedAtRef.current = Date.now();

      try {
        const context = new AudioContext();
        const analyser = context.createAnalyser();
        analyser.fftSize = 512;
        void context.resume().catch(() => {});
        context.createMediaStreamSource(stream).connect(analyser);
        audioContextRef.current = context;
        voiceActivityDetectionAvailableRef.current = true;
        const sampleData = new Uint8Array(analyser.fftSize);
        voiceSampleTimerRef.current = window.setInterval(() => {
          analyser.getByteTimeDomainData(sampleData);
          const rms = Math.sqrt(
            sampleData.reduce((sum, sample) => {
              const value = (sample - 128) / 128;
              return sum + value * value;
            }, 0) / sampleData.length,
          );
          if (rms >= VOICE_ACTIVITY_RMS_THRESHOLD) {
            activeVoiceMsRef.current += VOICE_SAMPLE_INTERVAL_MS;
          }
        }, VOICE_SAMPLE_INTERVAL_MS);
      } catch {
        // 无法读取音量时保留录音能力，由服务端识别空白结果。
      }

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          voiceChunksRef.current.push(event.data);
        }
      };
      recorder.onstop = () => {
        const chunks = voiceChunksRef.current;
        const durationMs = voiceDurationMsRef.current || Date.now() - voiceStartedAtRef.current;
        const activeVoiceMs = activeVoiceMsRef.current;
        const cancelled = voiceCancelledRef.current;
        const audioBlob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
        voiceChunksRef.current = [];
        releaseVoiceResources();

        if (cancelled) {
          return;
        }
        if (
          durationMs < MIN_VOICE_DURATION_MS ||
          (voiceActivityDetectionAvailableRef.current && activeVoiceMs < MIN_ACTIVE_VOICE_MS)
        ) {
          setVoiceError("录音过短或未检测到语音，未发送转写请求。");
          setVoiceState("idle");
          return;
        }
        if (audioBlob.size === 0 || audioBlob.size > MAX_VOICE_AUDIO_BYTES) {
          setVoiceError("录音文件过大或无效，请缩短后重试。");
          setVoiceState("idle");
          return;
        }

        void blobToBase64(audioBlob)
          .then((audioBase64) =>
            onTranscribeAudio({
              audioBase64,
              mimeType: audioBlob.type || "audio/webm",
              durationMs,
            }),
          )
          .then((text) => {
            if (!text.trim()) {
              throw new Error("未识别到可用文字，请重新录制。");
            }
            insertVoiceTranscript(text);
            setVoiceError(null);
          })
          .catch((error: unknown) => {
            setVoiceError(error instanceof Error ? error.message : "语音转写失败，请稍后重试。");
          })
          .finally(() => {
            setVoiceState("idle");
          });
      };
      recorder.start(500);
      setVoiceState("recording");
      voiceDisplayTimerRef.current = window.setInterval(() => {
        setVoiceElapsedSeconds(Math.floor((Date.now() - voiceStartedAtRef.current) / 1000));
      }, 250);
      voiceTimeoutRef.current = window.setTimeout(
        stopVoiceRecording,
        settings.voiceInput.maxDurationSeconds * 1000,
      );
    } catch (error) {
      releaseVoiceResources();
      setVoiceState("idle");
      setVoiceError(error instanceof Error ? `无法开始录音：${error.message}` : "无法开始录音。");
    }
  };

  useEffect(
    () => () => {
      voiceCancelledRef.current = true;
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = null;
        recorder.stop();
      }
      releaseVoiceResources();
    },
    [],
  );

  useEffect(() => {
    const stream = streamRef.current;
    if (!stream) {
      return;
    }

    const handleScroll = () => {
      shouldStickToBottomRef.current = isNearBottom(stream);
    };

    handleScroll();
    stream.addEventListener("scroll", handleScroll);
    return () => stream.removeEventListener("scroll", handleScroll);
  }, []);

  useEffect(() => {
    const stream = streamRef.current;
    const latestMessage = visibleMessages[visibleMessages.length - 1];
    const forceScrollForSolo = latestMessage?.mode === "solo";
    if (!stream || messageItems.length === 0 || (!shouldStickToBottomRef.current && !forceScrollForSolo)) {
      return;
    }
    requestAnimationFrame(() => {
      stream.scrollTo({ top: stream.scrollHeight, behavior: "auto" });
    });
  }, [messageItems.length, soloStatus.state, visibleMessages]);

  const toggleTrace = useCallback((traceKey: string) => {
    setExpandedTraceIds((current) => {
      const next = new Set(current);
      if (next.has(traceKey)) {
        next.delete(traceKey);
      } else {
        next.add(traceKey);
      }
      return next;
    });
  }, []);

  const toggleProcessGroup = useCallback((groupId: string) => {
    setExpandedProcessGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupId)) {
        next.delete(groupId);
      } else {
        next.add(groupId);
      }
      return next;
    });
  }, []);

  const handleFilesSelected = async (fileList: FileList | null) => {
    const files = Array.from(fileList ?? []);
    if (files.length === 0) {
      return;
    }
    setAttachmentError(null);
    const availableSlots = MAX_ATTACHMENTS_PER_MESSAGE - draftAttachments.length;
    if (availableSlots <= 0) {
      setAttachmentError(`单条消息最多 ${MAX_ATTACHMENTS_PER_MESSAGE} 个附件。`);
      return;
    }

    const selected = files.slice(0, availableSlots);
    if (files.length > availableSlots) {
      setAttachmentError(`已达到 ${MAX_ATTACHMENTS_PER_MESSAGE} 个附件上限，超出的文件未加入。`);
    }

    const next: AttachmentRef[] = [];
    for (const file of selected) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        next.push({
          id: createAttachmentId(),
          name: file.name,
          mimeType: file.type || "application/octet-stream",
          size: file.size,
          kind: attachmentKind(file),
          source: "local",
          status: "error",
          error: "超过 25MB 限制",
        });
        continue;
      }
      const contentBase64 = await fileToBase64(file);
      next.push({
        id: createAttachmentId(),
        name: file.name,
        mimeType: file.type || "application/octet-stream",
        size: file.size,
        kind: attachmentKind(file),
        source: "local",
        status: "pending",
        contentBase64,
        previewUrl: file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined,
      });
    }
    setDraftAttachments((current) => [...current, ...next]);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const removeDraftAttachment = (id: string) => {
    setDraftAttachments((current) => {
      const target = current.find((attachment) => attachment.id === id);
      if (target?.previewUrl) {
        URL.revokeObjectURL(target.previewUrl);
      }
      return current.filter((attachment) => attachment.id !== id);
    });
  };

  const submit = async () => {
    const normalized = draft.trim();
    const validAttachments = draftAttachments.filter((attachment) => attachment.status !== "error");
    if (!normalized && validAttachments.length === 0) {
      return;
    }

    onSend(normalized || "请处理这些附件。", publicAttachments(validAttachments));
    setDraft("");
    for (const attachment of draftAttachments) {
      if (attachment.previewUrl) {
        URL.revokeObjectURL(attachment.previewUrl);
      }
    }
    setDraftAttachments([]);
    setAttachmentError(null);
  };

  const applySlashItem = (item: SlashItem) => {
    if (!slashQuery) {
      return;
    }

    const nextDraft =
      draft.slice(0, slashQuery.slashIndex) +
      item.value +
      draft.slice(slashQuery.caretIndex);

    setDraft(nextDraft);
    setActiveIndex(0);

    requestAnimationFrame(() => {
      if (!textareaRef.current) {
        return;
      }

      const nextCaret = slashQuery.slashIndex + item.value.length;
      textareaRef.current.focus();
      textareaRef.current.setSelectionRange(nextCaret, nextCaret);
    });
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (voiceState === "recording" && event.key === "Escape") {
      event.preventDefault();
      cancelVoiceRecording();
      return;
    }
    if (slashQuery && flatItems.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((current) => (current + 1) % flatItems.length);
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((current) => (current - 1 + flatItems.length) % flatItems.length);
        return;
      }

      if (event.key === "Enter" && !event.altKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        applySlashItem(flatItems[activeIndex] ?? flatItems[0]);
        return;
      }

      if (event.key === "Escape") {
        event.preventDefault();
        setDraft((current) =>
          slashQuery
            ? current.slice(0, slashQuery.slashIndex) + current.slice(slashQuery.caretIndex)
            : current,
        );
        setActiveIndex(0);
        return;
      }
    }

    if (event.key !== "Enter" || event.nativeEvent.isComposing || event.altKey) {
      return;
    }

    event.preventDefault();
    void submit();
  };

  const permissionIsAll = settings.permissions.mode === "all";
  const voiceInputReady =
    settings.voiceInput.enabled &&
    Boolean(
      settings.voiceInput.apiKey.trim() &&
        settings.voiceInput.baseUrl.trim() &&
        settings.voiceInput.modelId.trim(),
    );

  return (
    <section className="chat-workspace motion-safe:animate-[eagle-panel-up_260ms_ease-out_both]">
      <div className="workspace-mobile-bar mobile-only">
        <button className="icon-button" onClick={onOpenMobileSidebar} type="button">
          <PanelLeftOpen size={16} />
        </button>
      </div>

      <div ref={streamRef} className="message-stream">
        {visibleMessages.length === 0 ? (
          <div className="empty-message-state">
            {!canSend ? (
              <>
                <div className="empty-message-icon is-loading">
                  <LoaderCircle size={24} />
                </div>
                <h2>正在连接本地后端</h2>
                <p>openEagle 正在启动或连接本地服务，连接完成后就可以发送任务。</p>
              </>
            ) : (
              <>
                <div className="empty-message-icon">
                  <MonitorSmartphone size={24} />
                </div>
                <h2>和主 Agent 对话，它会自己调度执行</h2>
                <p>输入 `/` 可快速插入 Tool、MCP 和 Skill 指令，右侧活动面板会展示更完整的执行细节。</p>
              </>
            )}
          </div>
        ) : (
          <div className="message-list">
            {messageItems.map((item) => (
              <div key={item.id} className="message-list-row">
                {item.kind === "tool-group" ? (
                  <ToolMessageGroup
                    messages={item.messages}
                    expandedTraceIds={expandedTraceIds}
                    onToggleTrace={toggleTrace}
                    isCollapsed={!expandedProcessGroups.has(item.id)}
                    onToggleCollapsed={() => toggleProcessGroup(item.id)}
                  />
                ) : item.kind === "solo-process-group" ? (
                  <SoloProcessMessageGroup
                    messages={item.messages}
                    isCollapsed={!expandedProcessGroups.has(item.id)}
                    onToggleCollapsed={() => toggleProcessGroup(item.id)}
                  />
                ) : (
                  <MessageArticle
                    collapseProcess={collapseProcess}
                    expandedTraceIds={expandedTraceIds}
                    isProcessCollapsed={!expandedProcessGroups.has(`assistant-process-${item.message.id}`)}
                    message={item.message}
                    onToggleProcessCollapsed={() => toggleProcessGroup(`assistant-process-${item.message.id}`)}
                    onToggleTrace={toggleTrace}
                    soloProcessMessages={item.soloProcessMessages ?? []}
                  />
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="composer-shell">
        {soloConfirmation ? (
          <div className="inline-alert warning">
            <div className="inline-alert-copy">
              <ShieldAlert size={16} />
              <span>{soloConfirmation.action} 需要确认</span>
            </div>
            <div className="inline-alert-actions">
              <button className="ghost-button" onClick={onAllowDangerousStep} type="button">
                允许
              </button>
              <button className="ghost-button danger" onClick={onRejectDangerousStep} type="button">
                拒绝
              </button>
            </div>
          </div>
        ) : null}

        {toolConfirmation ? (
          <div className="inline-alert danger">
            <div className="inline-alert-copy">
              <CircleAlert size={16} />
              <span>{toolConfirmation.name} 需要工具确认</span>
            </div>
            <div className="inline-alert-actions">
              <button className="ghost-button" onClick={onAllowToolConfirmation} type="button">
                允许
              </button>
              <button className="ghost-button danger" onClick={onRejectToolConfirmation} type="button">
                拒绝
              </button>
            </div>
          </div>
        ) : null}

        {soloLastError ? (
          <div className="inline-alert danger">
            <div className="inline-alert-copy">
              <CircleAlert size={16} />
              <span>{soloLastError}</span>
            </div>
          </div>
        ) : null}

        {isSoloBusy ? (
          <div className="solo-control-bar">
            <div className="solo-control-copy">
              <strong>桌面执行中</strong>
              <span>
                {soloStatus.stepCount}/{soloStatus.maxSteps} · {executionStateLabel(soloStatus.state)}
              </span>
            </div>
            <div className="solo-control-actions">
              {soloStatus.state === "running" ? (
                <button className="ghost-button" onClick={onSoloPause} type="button">
                  暂停
                </button>
              ) : null}
              {soloStatus.state === "paused" ? (
                <button className="ghost-button" onClick={onSoloResume} type="button">
                  继续
                </button>
              ) : null}
              <button className="ghost-button danger" onClick={onSoloStop} type="button">
                结束
              </button>
            </div>
          </div>
        ) : null}

        <div className="composer-frame transition-[border-color,box-shadow,transform] duration-200 ease-out">
          <input
            ref={fileInputRef}
            multiple
            onChange={(event) => {
              void handleFilesSelected(event.target.files);
            }}
            style={{ display: "none" }}
            type="file"
          />

          {draftAttachments.length > 0 ? (
            <AttachmentList
              attachments={draftAttachments}
              onRemove={removeDraftAttachment}
              removable
            />
          ) : null}
          {attachmentError ? <div className="attachment-error">{attachmentError}</div> : null}
          {voiceError ? <div className="attachment-error">{voiceError}</div> : null}

          <div className="composer-input-wrap">
            <textarea
              ref={textareaRef}
              className="composer-input"
              disabled={!canSend}
              onChange={(event) => {
                setDraft(event.target.value);
                setActiveIndex(0);
              }}
              onKeyDown={handleKeyDown}
              placeholder={
                canSend
                  ? "输入想聊的内容或要完成的任务..."
                  : "等待后端启动完成..."
              }
              rows={1}
              value={draft}
            />

            {slashQuery ? (
              <div className="slash-menu" role="listbox">
                <div className="slash-menu-header">
                  <strong>命令面板</strong>
                  <span>上下键选择，Enter 插入</span>
                </div>

                {groupedItems.length > 0 ? (
                  <div className="slash-group-list">
                    {groupedItems.map((group) => (
                      <div key={group.category} className="slash-group">
                        <div className="slash-group-title">{group.category}</div>
                        {group.items.map((item) => {
                          const itemIndex = flatItems.findIndex((entry) => entry.id === item.id);
                          const isActive = itemIndex === activeIndex;
                          return (
                            <button
                              key={item.id}
                              className={isActive ? "slash-item is-active" : "slash-item"}
                              onClick={() => applySlashItem(item)}
                              onMouseEnter={() => setActiveIndex(itemIndex)}
                              type="button"
                            >
                              <div className="slash-item-main">
                                <span>{item.label}</span>
                                <small>{item.enabled ? "已启用" : "未启用"}</small>
                              </div>
                              <span className="slash-item-meta">{item.sublabel}</span>
                            </button>
                          );
                        })}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="slash-empty">没有匹配项，继续输入关键词试试。</div>
                )}
              </div>
            ) : null}
          </div>

          <div className="composer-footer">
            <button
              className={permissionIsAll ? "permission-status is-all" : "permission-status"}
              onClick={() => onPermissionModeChange(permissionIsAll ? "default" : "all")}
              title={permissionIsAll ? "切回默认权限" : "切换到所有权限"}
              type="button"
            >
              {permissionIsAll ? <ShieldCheck size={13} /> : <ShieldAlert size={13} />}
              <span>{permissionIsAll ? "所有权限" : "默认权限"}</span>
              <ChevronDown size={13} />
            </button>
            <div className="composer-footer-spacer" />
            <button
              aria-label="添加附件"
              className="attach-button"
              disabled={!canSend || draftAttachments.length >= MAX_ATTACHMENTS_PER_MESSAGE}
              onClick={() => fileInputRef.current?.click()}
              title="添加附件"
              type="button"
            >
              <Paperclip size={14} />
            </button>
            {voiceState === "recording" ? (
              <span className="voice-recording-status" aria-live="polite">
                <span className="voice-recording-dot" />
                {formatVoiceDuration(voiceElapsedSeconds)}
              </span>
            ) : null}
            <button
              aria-label={
                voiceState === "recording"
                  ? "结束录音并转写"
                  : voiceState === "transcribing"
                    ? "正在转写语音"
                    : "开始语音输入"
              }
              className={voiceState === "recording" ? "attach-button voice-button is-recording" : "attach-button voice-button"}
              disabled={
                voiceState === "transcribing" ||
                (voiceState === "idle" && (!canSend || !voiceInputReady))
              }
              onClick={() => {
                if (voiceState === "recording") {
                  stopVoiceRecording();
                  return;
                }
                void startVoiceRecording();
              }}
              title={
                voiceState === "recording"
                  ? "结束录音并转写"
                  : voiceState === "transcribing"
                    ? "正在转写..."
                    : voiceInputReady
                      ? "开始语音输入"
                      : "请先在设置 → 语音输入中完成配置"
              }
              type="button"
            >
              {voiceState === "recording" ? <Square size={13} fill="currentColor" /> : <Mic size={14} />}
            </button>
            <button
              aria-label={isSoloBusy ? "停止桌面执行" : "发送消息"}
              className={isSoloBusy ? "send-button is-stop" : "send-button"}
              disabled={
                !canSend ||
                (!isSoloBusy &&
                  !draft.trim() &&
                  draftAttachments.every((attachment) => attachment.status === "error"))
              }
              onClick={() => {
                if (isSoloBusy) {
                  onSoloStop();
                  return;
                }
                void submit();
              }}
              title={isSoloBusy ? "停止桌面执行" : "发送消息"}
              type="button"
            >
              {isSoloBusy ? <SquareStop size={16} /> : <SendHorizonal size={16} />}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

export const ChatWorkspace = memo(ChatWorkspaceComponent);
