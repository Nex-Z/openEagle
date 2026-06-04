import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { convertFileSrc } from "../../lib/electron-bridge";
import {
  ChevronDown,
  CircleAlert,
  FileText,
  Image as ImageIcon,
  LoaderCircle,
  MonitorSmartphone,
  PanelLeftOpen,
  Paperclip,
  Play,
  SendHorizonal,
  ShieldAlert,
  ShieldCheck,
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

function formatTraceDuration(startedAt: string, completedAt?: string) {
  if (!completedAt) {
    return "进行中";
  }

  const duration = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(duration) || duration < 0) {
    return "刚刚";
  }
  if (duration < 1000) {
    return `${duration}ms`;
  }
  return `${(duration / 1000).toFixed(duration >= 10_000 ? 0 : 1)}s`;
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
    ? `正在执行 ${total} 项工具调用...`
    : failed
      ? `${failed} 项工具调用失败`
      : `已完成 ${total} 项工具调用`;

  return { completed, failed, running, total, label };
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
  const progress = status.total
    ? Math.max(status.running ? 8 : 0, Math.round(((status.completed + status.failed) / status.total) * 100))
    : 0;

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
            <span className="tool-summary-progress" aria-hidden="true">
              <span style={{ width: `${progress}%` }} />
            </span>
          </span>
          <ChevronDown
            size={13}
            className="trace-group-chevron"
            style={{ transform: groupCollapsed ? "none" : "rotate(180deg)" }}
          />
        </div>
        {!groupCollapsed ? (
          <div className="trace-timeline">
            {group.traces.map((trace) => {
              const traceKey = `${message.id}:${trace.id}`;
              const isExpanded = expandedTraceIds.has(traceKey);
              const statusClass =
                trace.status === "completed"
                  ? "is-completed"
                  : trace.status === "error"
                    ? "has-error"
                    : trace.status === "started"
                      ? "is-running"
                      : "";

              return (
                <div
                  key={trace.id}
                  className={`trace-step ${statusClass}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggleTrace(traceKey);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onToggleTrace(traceKey);
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
        ) : null}
      </div>
    );
  }

  const trace = group.traces[0];
  if (!trace) return null;
  const traceKey = `${message.id}:${trace.id}`;
  const isExpanded = expandedTraceIds.has(traceKey);
  const statusClass =
    trace.status === "completed"
      ? "is-completed"
      : trace.status === "error"
        ? "has-error"
        : trace.status === "started"
          ? "is-running"
          : "";

  return (
    <div className="trace-timeline">
      <div
        className={`trace-step ${statusClass}`}
        onClick={() => onToggleTrace(traceKey)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onToggleTrace(traceKey);
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
    </div>
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

  const allTraces = messages.flatMap((m) => m.traces ?? []);
  const hasError = allTraces.some((t) => t.status === "error");
  const isRunning = allTraces.some((t) => t.status === "started");
  const count = allTraces.length;
  const status = traceGroupStatus(allTraces);
  const progress = status.total
    ? Math.max(status.running ? 8 : 0, Math.round(((status.completed + status.failed) / status.total) * 100))
    : 0;

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
          <span className="tool-summary-progress" aria-hidden="true">
            <span style={{ width: `${progress}%` }} />
          </span>
        </span>
        <ChevronDown
          size={13}
          className="tool-group-chevron"
          style={{ transform: isCollapsed ? "none" : "rotate(180deg)" }}
        />
      </div>
      {!isCollapsed ? (
        <div className="trace-timeline">
          {messages.map((message) =>
            (message.traces ?? []).map((trace) => {
              const traceKey = `${message.id}:${trace.id}`;
              const isExpanded = expandedTraceIds.has(traceKey);
              const statusClass =
                trace.status === "completed"
                  ? "is-completed"
                  : trace.status === "error"
                    ? "has-error"
                    : trace.status === "started"
                      ? "is-running"
                      : "";

              return (
                <div
                  key={trace.id}
                  className={`trace-step ${statusClass}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggleTrace(traceKey);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onToggleTrace(traceKey);
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
            }),
          )}
        </div>
      ) : null}
    </article>
  );
}

export function ChatWorkspace(props: ChatWorkspaceProps) {
  const {
    messages,
    canSend,
    onSend,
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
  const [activeIndex, setActiveIndex] = useState(0);
  const [expandedTraceIds, setExpandedTraceIds] = useState<Set<string>>(new Set());
  const [expandedToolGroups, setExpandedToolGroups] = useState<Set<string>>(new Set());
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);

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

  const flatItems = groupedItems.flatMap((group) => group.items);

  const visibleMessages = useMemo(
    () =>
      messages.filter(
        (message) => !(message.mode === "solo" && (message.imagePath || message.role === "tool")),
      ),
    [messages],
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
    if (!stream || (!shouldStickToBottomRef.current && !forceScrollForSolo)) {
      return;
    }
    requestAnimationFrame(() => {
      stream.scrollTo({ top: stream.scrollHeight, behavior: "auto" });
    });
  }, [soloStatus.state, visibleMessages]);

  const toggleTrace = (traceKey: string) => {
    setExpandedTraceIds((current) => {
      const next = new Set(current);
      if (next.has(traceKey)) {
        next.delete(traceKey);
      } else {
        next.add(traceKey);
      }
      return next;
    });
  };

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

  const isSoloBusy =
    soloStatus.state === "running" ||
    soloStatus.state === "paused" ||
    soloStatus.state === "waiting_user_confirmation";
  const permissionIsAll = settings.permissions.mode === "all";

  return (
    <section className="chat-workspace bg-white motion-safe:animate-[eagle-panel-up_260ms_ease-out_both]">
      <div className="workspace-mobile-bar mobile-only">
        <button className="icon-button" onClick={onOpenMobileSidebar} type="button">
          <PanelLeftOpen size={16} />
        </button>
      </div>

      <div ref={streamRef} className="message-stream scroll-smooth">
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
          (() => {
            const elements: ReactNode[] = [];
            let toolBuffer: ChatMessage[] = [];

            const flushToolBuffer = () => {
              if (toolBuffer.length === 0) return;
              const groupId = `tool-group-${toolBuffer[0].id}-${toolBuffer[toolBuffer.length - 1].id}`;
              const isCollapsed = !expandedToolGroups.has(groupId);
              elements.push(
                <ToolMessageGroup
                  key={groupId}
                  messages={toolBuffer}
                  expandedTraceIds={expandedTraceIds}
                  onToggleTrace={toggleTrace}
                  isCollapsed={isCollapsed}
                  onToggleCollapsed={() => {
                    setExpandedToolGroups((prev) => {
                      const next = new Set(prev);
                      if (next.has(groupId)) {
                        next.delete(groupId);
                      } else {
                        next.add(groupId);
                      }
                      return next;
                    });
                  }}
                />,
              );
              toolBuffer = [];
            };

            for (const message of visibleMessages) {
              if (message.role === "tool" && message.traces && message.traces.length > 0) {
                toolBuffer.push(message);
                continue;
              }
              flushToolBuffer();

              elements.push(
                <article
                  key={message.id}
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

                  {shouldShowMessageLabel(message) ? (
                    <div className="message-label">{message.label}</div>
                  ) : null}

                  {message.blocks && message.blocks.length > 0 ? (
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
                            onToggleTrace={toggleTrace}
                          />
                        ) : (
                          <TraceGroup
                            key={block.id}
                            expandedTraceIds={expandedTraceIds}
                            group={{ id: `trace-group-${block.trace.id}`, kind: "trace-group", traces: [block.trace] }}
                            message={message}
                            onToggleTrace={toggleTrace}
                          />
                        ),
                      )}
                      {/* blocks 全是 trace 没有正文时，用 content 兜底 */}
                      {message.content &&
                        !message.blocks.some((b) => b.kind === "text" && b.content) && (
                          <div className="assistant-text-panel">
                            {renderMessageMarkdown(message.content)}
                          </div>
                        )}
                    </div>
                  ) : message.content ? (
                    renderMessageMarkdown(message.content)
                  ) : null}

                  <AttachmentList attachments={message.attachments} />

                  {(!message.blocks || message.blocks.length === 0) &&
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
                      onToggleTrace={toggleTrace}
                    />
                  ) : null}

                  {message.role === "assistant" && message.status === "pending" ? (
                    <div className="message-thinking" aria-label="AI 正在思考">
                      <span />
                      <span />
                      <span />
                    </div>
                  ) : null}
                </article>,
              );
            }
            flushToolBuffer();
            return elements;
          })()
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

        <div
          className="composer-frame transition-[border-color,box-shadow,transform] duration-200 ease-out"
          style={{ backdropFilter: "blur(8px)" }}
        >
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
              <div className="slash-menu" role="listbox" style={{ backdropFilter: "blur(8px)" }}>
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
            <button
              aria-label="发送消息"
              className="send-button"
              disabled={
                !canSend ||
                (!draft.trim() && draftAttachments.every((attachment) => attachment.status === "error"))
              }
              onClick={() => {
                void submit();
              }}
              type="button"
            >
              <SendHorizonal size={16} />
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
