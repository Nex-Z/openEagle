import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { invoke, listen, type UnlistenFn } from "../lib/electron-bridge";
import { registerMemorySnapshotSender } from "../lib/storage";
import { executionStateLabel, executionStatusLabel } from "../lib/runLabels";
import type {
  AgentExecutionTrace,
  AssistantMessageBlock,
  AttachmentRef,
  AppSettings,
  BackendState,
  ChatMessage,
  ConversationSummary,
  Envelope,
  ErrorPayload,
  IMStatusPayload,
  MemoryState,
  ScheduledTask,
  ScheduledTaskExecution,
  SoloConfirmationPayload,
  SoloDisplayOption,
  SoloControlPayload,
  SoloOverlayControlPayload,
  SoloPlanItem,
  SoloPlanStatus,
  SoloRunState,
  SoloScreenshotPayload,
  SoloStatusPayload,
  SoloStepPayload,
  StatusPayload,
  ToolConfirmationPayload,
  WechatBindStatusPayload,
} from "../types/protocol";

const BACKEND_EVENT = "backend://status";
const CONNECT_RETRY_LIMIT = 8;
const terminalSoloStates = new Set<SoloRunState>(["completed", "aborted", "error"]);
const activeSoloStates = new Set<SoloRunState>([
  "running",
  "paused",
  "waiting_user_confirmation",
]);

const emptyMemoryState: MemoryState = {
  profile: { content: "", updatedAt: "" },
  notes: [],
  agentSoul: { core: "", sideNotes: "", updatedAt: "" },
  audit: [],
  events: [],
};

function isElectronRuntime() {
  return typeof window !== "undefined" && "electronAPI" in window;
}

function createId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
}

function isTerminalSoloState(state: SoloRunState) {
  return terminalSoloStates.has(state);
}

function isSoloFinishAction(action: string) {
  return action.trim().toLowerCase() === "finish";
}

function compactOverlayText(text?: string, fallback = "", maxLength = 140) {
  const normalized = (text ?? "")
    .replace(/\r?\n/g, " ")
    .replace(/\s+/g, " ")
    .replace(/([A-Za-z]:\\[^\s]+|\/[^\s]+)/g, "[路径]")
    .trim();
  const value = normalized || fallback;
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

const SOLO_ACTION_LABELS: Record<string, string> = {
  observe: "观察屏幕",
  screenshot: "观察屏幕",
  click: "点击",
  double_click: "双击",
  type: "输入文本",
  key: "按键",
  hotkey: "快捷键",
  scroll: "滚动",
  wait: "等待",
  finish: "完成",
  navigate: "打开链接",
  open: "打开",
  drag: "拖拽",
};

function soloStepActionLabel(action?: string) {
  const normalized = action?.trim().toLowerCase();
  if (!normalized) {
    return "当前步骤";
  }
  return SOLO_ACTION_LABELS[normalized] ?? "执行动作";
}

function soloStepVisibleText(step: SoloStepPayload) {
  return compactOverlayText(
    step.agentMessage || step.expectedOutcome,
    "正在推进当前步骤。",
    120,
  );
}

function summarizeSoloDisplaysForLog(displays: unknown[]) {
  const formatNumber = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) ? value : "?";
  return displays
    .map((display) => {
      const item = display as Partial<SoloDisplayOption>;
      const selected = item.isSelected ? " selected" : "";
      return `#${formatNumber(item.index)} ${formatNumber(item.width)}x${formatNumber(
        item.height,
      )}@${formatNumber(item.left)},${formatNumber(item.top)}${selected}`;
    })
    .join("; ");
}

function buildOverlayPlanItems(
  plan: SoloPlanStatus | null,
): Array<{ index: number; status: SoloPlanItem["status"]; text: string }> {
  const items = plan?.items ?? [];
  if (items.length === 0) {
    return [];
  }

  const inProgressIndex = items.findIndex((item) => item.status === "in_progress");
  const pendingIndex = items.findIndex((item) => item.status === "pending");
  const startIndex =
    inProgressIndex >= 0
      ? inProgressIndex
      : pendingIndex >= 0
        ? pendingIndex
        : Math.max(0, items.length - 3);

  return items.slice(startIndex, startIndex + 3).map((item) => ({
    index: item.index,
    status: item.status,
    text: compactOverlayText(
      item.description || soloStepActionLabel(item.action),
      "待执行步骤",
      86,
    ),
  }));
}

function overlayDetailForStatus(
  status: SoloStatusPayload,
  confirmation: SoloConfirmationPayload | null,
) {
  switch (status.state) {
    case "running":
      return "正在执行桌面任务，保持目标窗口可见。";
    case "waiting_user_confirmation":
      return confirmation
        ? "需要你回到 openEagle 确认后继续。"
        : "正在等待你的确认。";
    case "paused":
      return compactOverlayText(status.detail, "执行已暂停，可回到 openEagle 继续。");
    case "completed":
      return compactOverlayText(status.detail, "桌面执行已完成。");
    case "aborted":
      return compactOverlayText(status.detail, "桌面执行已结束。");
    case "error":
      return compactOverlayText(status.detail, "桌面执行失败，请回到 openEagle 查看原因。");
    default:
      return compactOverlayText(status.detail, "请保持桌面可见。");
  }
}

function collectAssistantContent(blocks?: AssistantMessageBlock[]) {
  if (!blocks || blocks.length === 0) {
    return "";
  }
  return blocks
    .filter((block) => block.kind === "text")
    .map((block) => block.content)
    .join("\n\n");
}

function cloneAssistantBlocks(message?: ChatMessage): AssistantMessageBlock[] {
  if (message?.blocks && message.blocks.length > 0) {
    return message.blocks.map((block) =>
      block.kind === "text"
        ? { ...block }
        : { ...block, trace: { ...block.trace } },
    );
  }

  if (message?.content) {
    return [
      {
        id: createId("blk"),
        kind: "text",
        content: message.content,
        status: message.status === "pending" ? "pending" : "done",
        purpose: "final",
      },
    ];
  }

  return [];
}

function upsertAssistantMessage(
  current: ChatMessage[],
  requestId: string,
  updater: (message: ChatMessage | undefined) => ChatMessage,
) {
  const index = current.findIndex(
    (message) => message.role === "assistant" && message.requestId === requestId,
  );

  if (index === -1) {
    return [...current, updater(undefined)];
  }

  return current.map((message, messageIndex) =>
    messageIndex === index ? updater(message) : message,
  );
}

function appendChatMessage(current: ChatMessage[], message: ChatMessage) {
  return [...current, message];
}

function createChatMessage(params: {
  role: ChatMessage["role"];
  content: string;
  createdAt: string;
  requestId?: string;
  mode?: ChatMessage["mode"];
  status?: ChatMessage["status"];
  imagePath?: string;
  attachments?: AttachmentRef[];
  label?: string;
}) {
  return {
    id: createId(params.role),
    role: params.role,
    content: params.content,
    createdAt: params.createdAt,
    requestId: params.requestId,
    mode: params.mode,
    status: params.status,
    imagePath: params.imagePath,
    attachments: params.attachments,
    label: params.label,
  } satisfies ChatMessage;
}

function upsertAssistantTrace(
  current: ChatMessage[],
  requestId: string,
  trace: AgentExecutionTrace,
) {
  return upsertAssistantMessage(current, requestId, (message) => {
    const existingTraces = message?.traces ?? [];
    const nextTraces = existingTraces.some((item) => item.id === trace.id)
      ? existingTraces.map((item) =>
          item.id === trace.id
            ? {
                ...item,
                ...trace,
                startedAt: item.startedAt ?? trace.startedAt,
                completedAt: trace.completedAt ?? item.completedAt,
                params: trace.params ?? item.params,
                result: trace.result ?? item.result,
                summary: trace.summary ?? item.summary,
              }
            : item,
        )
      : [...existingTraces, trace];

    const blocks = cloneAssistantBlocks(message);
    const traceBlockIndex = blocks.findIndex(
      (block) => block.kind === "trace" && block.trace.id === trace.id,
    );

    if (traceBlockIndex >= 0) {
      const block = blocks[traceBlockIndex];
      if (block.kind === "trace") {
        block.trace = {
          ...block.trace,
          ...trace,
          startedAt: block.trace.startedAt ?? trace.startedAt,
          completedAt: trace.completedAt ?? block.trace.completedAt,
          params: trace.params ?? block.trace.params,
          result: trace.result ?? block.trace.result,
          summary: trace.summary ?? block.trace.summary,
        };
      }
    } else {
      blocks.push({
        id: `trace-${trace.id}`,
        kind: "trace",
        trace,
      });
    }

    return {
      id: message?.id ?? createId("assistant"),
      requestId,
      role: "assistant",
      content: collectAssistantContent(blocks),
      createdAt: message?.createdAt ?? trace.startedAt,
      status: message?.status ?? "pending",
      mode: message?.mode,
      label: message?.label,
      imagePath: message?.imagePath,
      attachments: message?.attachments,
      traces: nextTraces,
      blocks,
    };
  });
}

const initialState: BackendState = {
  phase: "starting",
  port: null,
  message: "正在启动本地后端...",
};

const idleSoloStatus: SoloStatusPayload = {
  state: "idle",
  stepCount: 0,
  maxSteps: 100,
};

export function useBackendConnection(
  conversationId: string,
  settings: AppSettings,
  initialMessages: ChatMessage[],
  onMessagesChange: (conversationId: string, messages: ChatMessage[]) => void,
  onConversationPatch: (
    conversationId: string,
    summary: ConversationSummary | undefined,
    updater: (messages: ChatMessage[]) => ChatMessage[],
  ) => void,
  onSettingsLoaded?: (settings: AppSettings) => void,
) {
  const [backend, setBackend] = useState<BackendState>(initialState);
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [statusLine, setStatusLine] = useState("后端服务启动中...");
  const [statusDetail, setStatusDetail] = useState<string | null>(null);
  const [soloStatus, setSoloStatus] = useState<SoloStatusPayload>(idleSoloStatus);
  const [soloStep, setSoloStep] = useState<SoloStepPayload | null>(null);
  const [soloConfirmation, setSoloConfirmation] =
    useState<SoloConfirmationPayload | null>(null);
  const [toolConfirmation, setToolConfirmation] =
    useState<ToolConfirmationPayload | null>(null);
  const [soloDisplays, setSoloDisplays] = useState<SoloDisplayOption[]>([]);
  const [soloTimeline, setSoloTimeline] = useState<string[]>([]);
  const [soloLastError, setSoloLastError] = useState<string | null>(null);
  const [soloPlan, setSoloPlan] = useState<SoloPlanStatus | null>(null);
  const [imStatuses, setImStatuses] = useState<
    Partial<Record<IMStatusPayload["provider"], IMStatusPayload>>
  >({});
  const [wechatBindStatus, setWechatBindStatus] =
    useState<WechatBindStatusPayload | null>(null);
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTask[]>([]);
  const [scheduledTaskHistory, setScheduledTaskHistory] = useState<
    Record<string, ScheduledTaskExecution[]>
  >({});
  const [memoryState, setMemoryState] = useState<MemoryState>(emptyMemoryState);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const retryCountRef = useRef(0);
  const activePortRef = useRef<number | null>(null);
  const onMessagesChangeRef = useRef(onMessagesChange);
  const messagesRef = useRef(messages);
  const syncedConversationIdRef = useRef(conversationId);
  const onConversationPatchRef = useRef(onConversationPatch);
  const skipNextMessageSyncRef = useRef(true);
  const activeSoloRequestIdRef = useRef<string | null>(null);
  const notifiedSoloRequestIdsRef = useRef<Set<string>>(new Set());
  const mainWindowFocusedRef = useRef(true);
  const userDismissedOverlayRef = useRef(false);
  const [overlayVisible, setOverlayVisible] = useState(false);
  const soloStatusRef = useRef(soloStatus);
  soloStatusRef.current = soloStatus;
  const soloStepRef = useRef(soloStep);
  soloStepRef.current = soloStep;
  const soloPlanRef = useRef(soloPlan);
  soloPlanRef.current = soloPlan;
  const soloConfirmationRef = useRef(soloConfirmation);
  soloConfirmationRef.current = soloConfirmation;
  messagesRef.current = messages;

  const syncSettings = () => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }

    const settingsEnvelope: Envelope<{ settings: AppSettings }> = {
      type: "client:update_settings",
      requestId: createId("settings"),
      conversationId,
      payload: { settings },
      timestamp: new Date().toISOString(),
    };
    socket.send(JSON.stringify(settingsEnvelope));
  };

  const requestMemoryState = () => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    const now = new Date().toISOString();
    const envelope: Envelope<Record<string, never>> = {
      type: "client:memory_get",
      requestId: createId("memory"),
      conversationId,
      payload: {},
      timestamp: now,
    };
    socket.send(JSON.stringify(envelope));
    return true;
  };

  const sendMemorySnapshot = (snapshot: {
    reason: string;
    content: string;
    source: string;
  }) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    const envelope: Envelope<typeof snapshot> = {
      type: "client:memory_ingest_snapshot",
      requestId: createId("memory-snapshot"),
      conversationId,
      payload: snapshot,
      timestamp: new Date().toISOString(),
    };
    socket.send(JSON.stringify(envelope));
    return true;
  };

  useEffect(() => registerMemorySnapshotSender(sendMemorySnapshot), [conversationId]);

  const appendSoloTimeline = (line: string) => {
    const stamped = `[${new Date().toLocaleTimeString()}] ${line}`;
    setSoloTimeline((current) => [...current.slice(-119), stamped]);
  };

  const appendSoloMessage = (message: Omit<ChatMessage, "id">) => {
    setMessages((current) =>
      appendChatMessage(current, {
        ...message,
        id: createId(message.role),
      }),
    );
  };

  const sendSoloControl = (payload: SoloControlPayload) => {
    const socket = socketRef.current;
    const requestId = payload.soloRequestId ?? activeSoloRequestIdRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN || !requestId) {
      return false;
    }
    const now = new Date().toISOString();
    const envelope: Envelope<SoloControlPayload> = {
      type: "client:solo_control",
      requestId,
      conversationId,
      payload: {
        ...payload,
        soloRequestId: requestId,
      },
      timestamp: now,
    };
    socket.send(JSON.stringify(envelope));
    return true;
  };

  const requestSoloDisplays = useCallback(() => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      console.warn("[ws] requestSoloDisplays skipped: socket not open");
      return false;
    }
    const now = new Date().toISOString();
    const requestId = createId("solo-displays");
    const envelope: Envelope<Record<string, never>> = {
      type: "client:list_solo_displays",
      requestId,
      conversationId,
      payload: {},
      timestamp: now,
    };
    console.log("[ws] sending client:list_solo_displays", requestId);
    socket.send(JSON.stringify(envelope));
    return true;
  }, [conversationId]);

  const sendWechatControl = (
    type: "client:wechat_bind_start" | "client:wechat_bind_cancel" | "client:wechat_unbind",
    payload: Record<string, unknown> = {},
  ) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setStatusLine("后端服务未就绪");
      setStatusDetail("当前连接尚未建立完成，微信绑定指令未发送。");
      return false;
    }
    const now = new Date().toISOString();
    const envelope: Envelope<Record<string, unknown>> = {
      type,
      requestId: createId("wechat"),
      conversationId,
      payload,
      timestamp: now,
    };
    socket.send(JSON.stringify(envelope));
    return true;
  };

  useEffect(() => {
    if (!isElectronRuntime()) {
      setBackend({
        phase: "error",
        port: null,
        message: "当前不在 Electron 环境，请通过 `pnpm electron:dev` 启动。",
      });
      setStatusLine("后端服务异常");
      setStatusDetail("当前不在 Electron 环境，请通过 `pnpm electron:dev` 启动。");
      return;
    }

    let unlisten: UnlistenFn | undefined;
    let mounted = true;

    const syncState = async () => {
      const next = await invoke<BackendState>("get_backend_state");
      if (mounted) {
        setBackend(next);
      }
    };

    void syncState();

    void listen<BackendState>(BACKEND_EVENT, (event) => {
      if (mounted) {
        setBackend(event.payload);
      }
    }).then((fn) => {
      unlisten = fn;
    });

    return () => {
      mounted = false;
      if (unlisten) {
        void unlisten();
      }
    };
  }, []);

  useEffect(() => {
    syncSettings();
  }, [conversationId, settings]);

  useEffect(() => {
    onMessagesChangeRef.current = onMessagesChange;
  }, [onMessagesChange]);

  useLayoutEffect(() => {
    const conversationChanged = syncedConversationIdRef.current !== conversationId;
    const externalMessagesChanged = initialMessages !== messagesRef.current;
    if (!conversationChanged && !externalMessagesChanged) {
      return;
    }
    syncedConversationIdRef.current = conversationId;
    skipNextMessageSyncRef.current = true;
    setMessages(initialMessages);
  }, [conversationId, initialMessages]);

  useEffect(() => {
    onConversationPatchRef.current = onConversationPatch;
  }, [onConversationPatch]);

  useLayoutEffect(() => {
    if (skipNextMessageSyncRef.current) {
      skipNextMessageSyncRef.current = false;
      return;
    }

    onMessagesChangeRef.current(conversationId, messages);
  }, [conversationId, messages]);

  useEffect(() => {
    if (!backend.port) {
      return;
    }
    if (backend.phase !== "ready" && backend.phase !== "disconnected") {
      return;
    }
    if (activePortRef.current === backend.port && socketRef.current) {
      return;
    }

    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
    }

    const targetPort = backend.port;
    activePortRef.current = targetPort;
    setStatusLine("正在连接后端服务");
    setStatusDetail(null);

    const socket = new WebSocket(`ws://127.0.0.1:${targetPort}/ws`);
    socketRef.current = socket;

    socket.addEventListener("open", () => {
      retryCountRef.current = 0;
      setBackend((current) => ({
        ...current,
        phase: "connected",
        port: targetPort,
        message: "连接已建立",
      }));
      setStatusLine("已连接后端服务");
      setStatusDetail(null);
      requestMemoryState();
      // Delay settings sync to let backend send persisted settings first
      setTimeout(syncSettings, 600);
    });

    socket.addEventListener("close", () => {
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
      activePortRef.current = null;

      const nextRetry = retryCountRef.current + 1;
      retryCountRef.current = nextRetry;

      if (nextRetry <= CONNECT_RETRY_LIMIT) {
        setBackend((current) => ({
          ...current,
          phase: "disconnected",
          port: targetPort,
          message: `连接中断，准备第 ${nextRetry} 次重试...`,
        }));
        setStatusLine("后端连接中断");
        setStatusDetail(`WebSocket 已断开，正在准备第 ${nextRetry} 次自动重试。`);
        reconnectTimerRef.current = window.setTimeout(() => {
          setBackend((current) => ({
            ...current,
            phase: "ready",
            port: targetPort,
            message: "准备重新连接后端...",
          }));
        }, Math.min(500 * nextRetry, 2200));
        return;
      }

      setBackend((current) => ({
        ...current,
        phase: "error",
        port: targetPort,
        message: "WebSocket 连接失败，请检查后端启动日志。",
      }));
      setStatusLine("后端服务异常");
      setStatusDetail("WebSocket 连接失败，请检查后端启动日志。");
    });

    socket.addEventListener("error", () => {
      setStatusLine("后端服务异常");
      setStatusDetail("WebSocket 连接异常，正在等待自动重试。");
    });

    socket.addEventListener("message", (event) => {
      const envelope = JSON.parse(event.data) as Envelope<
        {
          content?: string;
          answer?: string;
          route?: string;
          attachments?: AttachmentRef[];
          detail?: string;
          trace?: AgentExecutionTrace;
          status?: SoloStatusPayload;
          step?: SoloStepPayload;
          confirmation?: SoloConfirmationPayload | ToolConfirmationPayload;
          displays?: SoloDisplayOption[];
          screenshot?: SoloScreenshotPayload;
          label?: string;
          preferredDisplayIndex?: number;
          conversation?: ConversationSummary;
          source?: string;
          provider?: IMStatusPayload["provider"];
          lastBlockedOpenId?: string;
          lastBlockedChatId?: string;
          state?: IMStatusPayload["state"] | WechatBindStatusPayload["state"];
          message?: string;
          qrcodeUrl?: string;
          accountId?: string;
          userId?: string;
          tasks?: ScheduledTask[];
          task?: ScheduledTask;
          taskId?: string;
          executions?: ScheduledTaskExecution[];
          taskName?: string;
          result?: string;
          error?: string;
        } & ErrorPayload &
          StatusPayload
      >;

      const targetConversationId = envelope.conversationId;
      const targetConversation = envelope.payload.conversation;
      const patchMessages = (updater: (messages: ChatMessage[]) => ChatMessage[]) => {
        if (targetConversationId === conversationId) {
          setMessages(updater);
          return;
        }
        onConversationPatchRef.current(
          targetConversationId,
          targetConversation,
          updater,
        );
      };
      const appendEnvelopeMessage = (message: Omit<ChatMessage, "id">) => {
        patchMessages((current) =>
          appendChatMessage(current, {
            ...message,
            id: createId(message.role),
          }),
        );
      };

      if (envelope.type === "server:im_status") {
        if (envelope.payload.provider && envelope.payload.state) {
          const nextStatus: IMStatusPayload = {
            provider: envelope.payload.provider,
            state: envelope.payload.state as IMStatusPayload["state"],
            detail: envelope.payload.detail,
            lastBlockedOpenId: envelope.payload.lastBlockedOpenId,
            lastBlockedChatId: envelope.payload.lastBlockedChatId,
          };
          setImStatuses((current) => ({
            ...current,
            [nextStatus.provider]: nextStatus,
          }));
        }
        return;
      }

      if (envelope.type === "server:wechat_bind_status") {
        const payload = envelope.payload as unknown as WechatBindStatusPayload;
        if (payload.state && payload.message) {
          setWechatBindStatus((current) => ({
            ...payload,
            qrcodeUrl:
              payload.qrcodeUrl ??
              (payload.state === "waiting" ? current?.qrcodeUrl : undefined),
          }));
        }
        return;
      }

      if (envelope.type === "server:memory_state" || envelope.type === "server:memory_updated") {
        setMemoryState(envelope.payload as unknown as MemoryState);
        return;
      }

      if (envelope.type === "server:settings_loaded") {
        const backendSettings = (envelope.payload as unknown as Record<string, unknown>).settings;
        if (backendSettings && typeof backendSettings === "object" && onSettingsLoaded) {
          onSettingsLoaded(backendSettings as AppSettings);
        }
        return;
      }

      if (envelope.type === "server:external_user_message") {
        appendEnvelopeMessage({
          requestId: envelope.requestId,
          role: "user",
          label:
            envelope.payload.source === "feishu"
              ? "飞书"
              : envelope.payload.source === "telegram"
                ? "Telegram"
                : envelope.payload.source === "wechat"
                  ? "微信"
                  : "IM",
          content: envelope.payload.content ?? "",
          attachments: envelope.payload.attachments,
          createdAt: envelope.timestamp,
          status: "done",
        });
        return;
      }

      if (envelope.type === "server:attachments_ready") {
        const attachments = envelope.payload.attachments ?? [];
        patchMessages((current) =>
          current.map((message) =>
            message.requestId === envelope.requestId && message.role === "user"
              ? { ...message, attachments }
              : message,
          ),
        );
        return;
      }

      if (envelope.type === "server:agent_progress") {
        const progress = envelope.payload.content?.trim() ?? "";
        if (!progress) {
          return;
        }
        patchMessages((current) =>
          upsertAssistantMessage(current, envelope.requestId, (message) => {
            const blocks = cloneAssistantBlocks(message);
            const last = blocks[blocks.length - 1];
            if (
              last &&
              last.kind === "text" &&
              last.purpose === "progress" &&
              last.content === progress
            ) {
              return {
                id: message?.id ?? createId("assistant"),
                requestId: envelope.requestId,
                role: "assistant",
                content: collectAssistantContent(blocks),
                createdAt: message?.createdAt ?? envelope.timestamp,
                status: "pending",
                attachments: message?.attachments,
                traces: message?.traces ?? [],
                blocks,
              };
            }

            blocks.push({
              id: createId("blk"),
              kind: "text",
              content: progress,
              status: "done",
              purpose: "progress",
            });

            return {
              id: message?.id ?? createId("assistant"),
              requestId: envelope.requestId,
              role: "assistant",
              content: collectAssistantContent(blocks),
              createdAt: message?.createdAt ?? envelope.timestamp,
              status: "pending",
              attachments: message?.attachments,
              traces: message?.traces ?? [],
              blocks,
            };
          }),
        );
        setStatusLine("AI 正在处理");
        setStatusDetail(progress);
        return;
      }

      if (envelope.type === "server:message") {
        patchMessages((current) =>
          upsertAssistantMessage(current, envelope.requestId, (message) => {
            const visibleContent =
              envelope.payload.route === "answer_directly" && envelope.payload.answer
                ? envelope.payload.answer
                : envelope.payload.content;
            const blocks = cloneAssistantBlocks(message);
            const finalBlockIndex = blocks.findIndex(
              (block) => block.kind === "text" && block.purpose !== "progress",
            );
            if (visibleContent) {
              if (finalBlockIndex >= 0) {
                const block = blocks[finalBlockIndex];
                if (block.kind === "text") {
                  block.content = visibleContent;
                  block.status = "done";
                  block.purpose = "final";
                }
              } else {
                const finalBlock: AssistantMessageBlock = {
                  id: createId("blk"),
                  kind: "text",
                  content: visibleContent,
                  status: "done",
                  purpose: "final",
                };
                const hasProgressBlock = blocks.some(
                  (block) => block.kind === "text" && block.purpose === "progress",
                );
                if (hasProgressBlock) {
                  blocks.push(finalBlock);
                } else {
                  blocks.unshift(finalBlock);
                }
              }
            }

            for (const block of blocks) {
              if (block.kind === "text") {
                block.status = "done";
                block.purpose = block.purpose ?? "final";
              }
            }

            const content = collectAssistantContent(blocks) || visibleContent || "";
            return {
              id: message?.id ?? createId("assistant"),
              requestId: envelope.requestId,
              role: "assistant",
              content,
              createdAt: message?.createdAt ?? envelope.timestamp,
              status: "done",
              attachments: envelope.payload.attachments ?? message?.attachments,
              traces: message?.traces ?? [],
              blocks,
            };
          }),
        );
        setStatusLine("已连接后端服务");
        setStatusDetail(null);
        return;
      }

      if (envelope.type === "server:message_delta") {
        patchMessages((current) =>
          upsertAssistantMessage(current, envelope.requestId, (message) => {
            const delta = envelope.payload.content ?? "";
            const blocks = cloneAssistantBlocks(message);
            const last = blocks[blocks.length - 1];
            if (last && last.kind === "text" && last.status !== "done") {
              last.content += delta;
              last.status = "pending";
            } else {
              blocks.push({
                id: createId("blk"),
                kind: "text",
                content: delta,
                status: "pending",
                purpose: "final",
              });
            }

            return {
              id: message?.id ?? createId("assistant"),
              requestId: envelope.requestId,
              role: "assistant",
              content: collectAssistantContent(blocks),
              createdAt: message?.createdAt ?? envelope.timestamp,
              status: "pending",
              traces: message?.traces ?? [],
              blocks,
            };
          }),
        );
        setStatusLine("AI 正在输出");
        setStatusDetail(null);
        return;
      }

      if (envelope.type === "server:status") {
        if (envelope.payload.stage === "thinking") {
          patchMessages((current) =>
            upsertAssistantMessage(current, envelope.requestId, (message) => {
              const blocks = cloneAssistantBlocks(message);
              return {
                id: message?.id ?? createId("assistant"),
                requestId: envelope.requestId,
                role: "assistant",
                content: collectAssistantContent(blocks),
                createdAt: message?.createdAt ?? envelope.timestamp,
                status: "pending",
                traces: message?.traces ?? [],
                blocks,
              };
            }),
          );
          setStatusLine("AI 正在思考");
          setStatusDetail(envelope.payload.detail ?? null);
          return;
        }

        if (envelope.payload.stage === "idle") {
          patchMessages((current) =>
            current.map((message) =>
              message.role === "assistant" &&
              message.requestId === envelope.requestId &&
              message.status === "pending"
                ? { ...message, status: "done" }
                : message,
            ),
          );
          setStatusLine("已连接后端服务");
          setStatusDetail(null);
          return;
        }

        setStatusLine("已连接后端服务");
        setStatusDetail(envelope.payload.detail ?? null);
        return;
      }

      if (envelope.type === "server:trace" && envelope.payload.trace) {
        const trace = envelope.payload.trace!;
        if (envelope.requestId === activeSoloRequestIdRef.current) {
          appendEnvelopeMessage({
            requestId: envelope.requestId,
            role: "tool",
            label: trace.name,
            content: trace.summary ?? "",
            createdAt: trace.completedAt ?? trace.startedAt,
            status: trace.status === "error" ? "error" : "done",
            mode: "solo",
            traces: [trace],
            blocks: [
              {
                id: `trace-${trace.id}`,
                kind: "trace",
                trace,
              },
            ],
          });
        } else {
          patchMessages((current) =>
            upsertAssistantTrace(current, envelope.requestId, trace),
          );
        }
        return;
      }

      if (
        envelope.type === "server:tool_confirmation_required" &&
        envelope.payload.confirmation
      ) {
        const confirmation = envelope.payload.confirmation as ToolConfirmationPayload;
        setToolConfirmation(confirmation);
        setStatusLine("等待工具确认");
        setStatusDetail(`${confirmation.name}: ${confirmation.reason}`);
        appendEnvelopeMessage({
            requestId: envelope.requestId,
            role: "system",
            label: "工具确认",
            content: `工具 \`${confirmation.name}\` 需要确认。\n\n原因: ${confirmation.reason}`,
            createdAt: envelope.timestamp,
            status: "error" as const,
        });
        return;
      }

      if (envelope.type === "server:solo_displays") {
        const displays = Array.isArray(envelope.payload.displays) ? envelope.payload.displays : [];
        console.log(
          "[ws] received server:solo_displays",
          `count=${displays.length}`,
          summarizeSoloDisplaysForLog(displays),
        );
        setSoloDisplays(displays);
        return;
      }

      if (envelope.type === "server:solo_screenshot" && envelope.payload.screenshot) {
        const screenshot = envelope.payload.screenshot;
        activeSoloRequestIdRef.current = envelope.requestId;
        patchMessages((current) =>
          appendChatMessage(current, createChatMessage({
            role: "tool",
            label: envelope.payload.label || "截图预览",
            content: "已捕获新的屏幕状态。",
            createdAt: screenshot.capturedAt ?? envelope.timestamp,
            requestId: envelope.requestId,
            mode: "solo",
            imagePath: screenshot.path,
            status: "done",
          })),
        );
        return;
      }

      if (envelope.type === "server:solo_status" && envelope.payload.status) {
        activeSoloRequestIdRef.current = envelope.requestId;
        const nextStatus = envelope.payload.status;
        setSoloStatus((current) => ({
          ...nextStatus,
          // Preserve detail from solo_step finish handler if solo_status has none
          detail: nextStatus.detail || current.detail,
        }));
        appendSoloTimeline(
          `状态更新: ${executionStateLabel(nextStatus.state)}${nextStatus.detail ? ` · ${nextStatus.detail}` : ""}`,
        );
        if (
          nextStatus.detail &&
          (nextStatus.state === "paused" ||
            nextStatus.state === "completed" ||
            nextStatus.state === "aborted" ||
            nextStatus.state === "error")
        ) {
          const statusDetailText = nextStatus.detail;
          patchMessages((current) =>
            appendChatMessage(current, createChatMessage({
              role: nextStatus.state === "error" ? "system" : "assistant",
              label: executionStatusLabel(nextStatus.state),
              content: statusDetailText,
              createdAt: new Date().toISOString(),
              requestId: envelope.requestId,
              mode: "solo",
              status: nextStatus.state === "error" ? "error" : "done",
            })),
          );
        }
        if (nextStatus.state === "running") {
          setSoloLastError(null);
        }
        if (nextStatus.state === "error" || nextStatus.state === "paused") {
          if (nextStatus.detail?.includes("失败") || nextStatus.detail?.includes("异常")) {
            setSoloLastError(nextStatus.detail);
          }
        }
        if (
          nextStatus.state === "completed" ||
          nextStatus.state === "aborted" ||
          nextStatus.state === "error"
        ) {
          setSoloConfirmation(null);
        }
        return;
      }

      if (envelope.type === "server:solo_step" && envelope.payload.step) {
        const step = envelope.payload.step;
        const visibleText = soloStepVisibleText(step);
        activeSoloRequestIdRef.current = envelope.requestId;
        setSoloStep(step);
        appendSoloTimeline(
          `正在处理: ${visibleText}`,
        );
        patchMessages((current) =>
          appendChatMessage(current, createChatMessage({
            role: "assistant",
            content: visibleText,
            createdAt: step.timestamp,
            requestId: envelope.requestId,
            mode: "solo",
            status: "done",
          })),
        );
        appendEnvelopeMessage({
          requestId: envelope.requestId,
          role: "tool",
          label: step.action,
          content: step.expectedOutcome ?? "",
          createdAt: step.timestamp,
          status: "done",
          mode: "solo",
          traces: [
            {
              id: `solo-step-${envelope.requestId}-${step.stepIndex}`,
              kind: "tool",
              name: step.action,
              status: "started",
              summary: step.expectedOutcome,
              params: step.actionArgs ?? {},
              startedAt: step.timestamp,
            },
          ],
          blocks: [
            {
              id: `trace-solo-step-${envelope.requestId}-${step.stepIndex}`,
              kind: "trace",
              trace: {
                id: `solo-step-${envelope.requestId}-${step.stepIndex}`,
                kind: "tool",
                name: step.action,
                status: "started",
                summary: step.expectedOutcome,
                params: step.actionArgs ?? {},
                startedAt: step.timestamp,
              },
            },
          ],
        });
        if (step.screenshotPath) {
          patchMessages((current) =>
            appendChatMessage(current, createChatMessage({
              role: "tool",
              label: "截图预览",
              content: "当前屏幕状态已更新，正在继续处理。",
              createdAt: step.timestamp,
              requestId: envelope.requestId,
              mode: "solo",
              imagePath: step.screenshotPath,
              status: "done",
            })),
          );
        }
        if (isSoloFinishAction(step.action)) {
          setSoloConfirmation(null);
          setSoloLastError(null);
          setSoloStatus((current) => {
            if (
              current.state === "completed" ||
              current.state === "aborted" ||
              current.state === "error"
            ) {
              return current;
            }
            return {
              ...current,
              state: "completed",
              detail: "任务已完成，返回 openEagle 查看执行结果。",
              stepCount: Math.max(current.stepCount, step.stepIndex),
              maxSteps: Math.max(current.maxSteps, step.stepIndex),
              lastAction: step.action,
              completedAt: step.timestamp,
            };
          });
        }
        return;
      }

      if (envelope.type === "server:solo_plan" && (envelope.payload as unknown as Record<string, unknown>).plan) {
        setSoloPlan((envelope.payload as unknown as Record<string, unknown>).plan as SoloPlanStatus);
        return;
      }

      if (
        envelope.type === "server:solo_confirmation_required" &&
        envelope.payload.confirmation
      ) {
        const confirmation = envelope.payload.confirmation as SoloConfirmationPayload;
        activeSoloRequestIdRef.current = envelope.requestId;
        setSoloConfirmation(confirmation);
        setSoloStatus((current) => ({
          ...current,
          state: "waiting_user_confirmation",
        }));
        appendSoloTimeline(
          `等待确认: ${confirmation.action} · ${confirmation.reason}`,
        );
        patchMessages((current) =>
          appendChatMessage(current, createChatMessage({
            role: "system",
            label: "危险动作确认",
            content: `${confirmation.thoughtSummary}\n\n动作: \`${confirmation.action}\`\n\n原因: ${confirmation.reason}`,
            createdAt: new Date().toISOString(),
            requestId: envelope.requestId,
            mode: "solo",
            status: "error",
          })),
        );
        return;
      }

      if (envelope.type === "server:scheduled_tasks") {
        const tasks = (envelope.payload.tasks ?? []) as ScheduledTask[];
        setScheduledTasks(tasks);
        return;
      }

      if (envelope.type === "server:scheduled_task_created") {
        const task = envelope.payload.task as ScheduledTask | undefined;
        if (task) {
          setScheduledTasks((current) => [task, ...current]);
        }
        return;
      }

      if (envelope.type === "server:scheduled_task_updated") {
        const task = envelope.payload.task as ScheduledTask | undefined;
        if (task) {
          setScheduledTasks((current) =>
            current.map((t) => (t.id === task.id ? task : t)),
          );
        }
        return;
      }

      if (envelope.type === "server:scheduled_task_deleted") {
        const taskId = envelope.payload.taskId as string;
        setScheduledTasks((current) => current.filter((t) => t.id !== taskId));
        return;
      }

      if (envelope.type === "server:scheduled_task_history") {
        const taskId = envelope.payload.taskId as string;
        const executions = (envelope.payload.executions ?? []) as ScheduledTaskExecution[];
        setScheduledTaskHistory((current) => ({ ...current, [taskId]: executions }));
        return;
      }

      if (envelope.type === "server:scheduled_task_executed") {
        const taskName = (envelope.payload.taskName as string) ?? "定时任务";
        const result = (envelope.payload.result as string) ?? "";
        const error = envelope.payload.error as string | undefined;
        appendEnvelopeMessage({
          requestId: envelope.requestId,
          role: "assistant",
          label: "定时任务执行结果",
          content: `【${taskName}】\n\n${error ?? result}`,
          createdAt: envelope.timestamp,
          status: error ? "error" : "done",
        });
        return;
      }

      if (envelope.type === "server:error") {
        setStatusLine("后端服务异常");
        setStatusDetail(
          envelope.payload.detail ?? envelope.payload.message ?? "未知错误",
        );
        patchMessages((current) => {
          const next: ChatMessage[] = current.map((message) =>
            message.role === "assistant" &&
            message.requestId === envelope.requestId &&
            message.status === "pending"
              ? { ...message, status: "error" as const }
              : message,
          );

          return [
            ...next,
            {
              id: createId("system"),
              role: "system",
              content: envelope.payload.message ?? "未知错误",
              createdAt: envelope.timestamp,
              status: "error" as const,
            },
          ];
        });
      }
    });
  }, [backend.phase, backend.port, conversationId, settings]);

  const buildOverlayPayload = () => {
    const status = soloStatusRef.current;
    const step = soloStepRef.current;
    const confirmation = soloConfirmationRef.current;

    return {
      title: "正在执行桌面任务",
      detail: overlayDetailForStatus(status, confirmation),
      stepText:
        status.state === "waiting_user_confirmation"
          ? "等待你确认后继续。"
          : step
            ? soloStepVisibleText(step)
            : undefined,
      stepLabel:
        status.state === "waiting_user_confirmation"
          ? "等待确认"
          : step
            ? soloStepActionLabel(step.action)
            : undefined,
      historyText: undefined,
      state: status.state,
      stepCount: status.stepCount,
      maxSteps: status.maxSteps,
      planItems: buildOverlayPlanItems(soloPlanRef.current),
      confirmationAction: confirmation
        ? soloStepActionLabel(confirmation.action)
        : undefined,
      confirmationReason: confirmation
        ? compactOverlayText(confirmation.reason, "请确认是否继续。", 120)
        : undefined,
    };
  };

  useEffect(() => {
    if (!isElectronRuntime()) return;

    const unlisten = listen<boolean>("main://focus_changed", (event) => {
      const focused = event.payload;
      mainWindowFocusedRef.current = focused;

      if (focused) {
        userDismissedOverlayRef.current = false;
        setOverlayVisible(false);
        void invoke("hide_solo_overlay").catch(() => {});
      } else {
        const s = soloStatusRef.current;
        if (activeSoloStates.has(s.state) && !userDismissedOverlayRef.current) {
          setOverlayVisible(true);
          void invoke("show_solo_overlay", { payload: buildOverlayPayload() }).catch(() => {});
        }
      }
    });

    return () => {
      unlisten.then((fn) => fn());
    };
  }, []);

  useEffect(() => {
    if (!isElectronRuntime()) return;
    const unlisten = listen("solo://user_dismissed", () => {
      userDismissedOverlayRef.current = true;
      setOverlayVisible(false);
      void invoke("hide_solo_overlay").catch(() => {});
    });
    return () => {
      unlisten.then((fn) => fn());
    };
  }, []);

  useEffect(() => {
    if (!isElectronRuntime()) return;
    const unlisten = listen<SoloOverlayControlPayload>(
      "solo://overlay_control",
      (event) => {
        const action = event.payload?.action;
        switch (action) {
          case "pause":
          case "resume":
          case "stop":
            sendSoloControl({ action });
            break;
          case "confirm_allow":
            setSoloConfirmation(null);
            sendSoloControl({ action: "confirm_allow" });
            break;
          case "confirm_reject":
            setSoloConfirmation(null);
            sendSoloControl({ action: "confirm_reject" });
            break;
          case "dismiss":
            userDismissedOverlayRef.current = true;
            setOverlayVisible(false);
            void invoke("hide_solo_overlay").catch(() => {});
            break;
          default:
            break;
        }
      },
    );
    return () => {
      unlisten.then((fn) => fn());
    };
  }, [conversationId]);

  useEffect(() => {
    if (!isElectronRuntime()) {
      return;
    }

    const payload = buildOverlayPayload();
    const focused = mainWindowFocusedRef.current;

    if (activeSoloStates.has(soloStatus.state)) {
      if (!focused && !userDismissedOverlayRef.current) {
        setOverlayVisible(true);
        void invoke("show_solo_overlay", { payload }).catch(
          (err) => console.error("[SOLO] show_solo_overlay failed:", err),
        );
      } else if (!focused) {
        void invoke("update_solo_overlay", { payload }).catch(
          (err) => console.error("[SOLO] update_solo_overlay failed:", err),
        );
      }
    } else if (soloStatus.state !== "idle") {
      void invoke("update_solo_overlay", { payload }).catch(
        (err) => console.error("[SOLO] update_solo_overlay failed:", err),
      );
    }

    if (isTerminalSoloState(soloStatus.state)) {
      const requestId =
        activeSoloRequestIdRef.current ??
        soloStatus.startedAt ??
        `${soloStatus.state}-${soloStatus.completedAt ?? ""}`;
      if (!notifiedSoloRequestIdsRef.current.has(requestId)) {
        notifiedSoloRequestIdsRef.current.add(requestId);
        void invoke("notify_solo_result", {
          payload: {
            requestId,
            state: soloStatus.state,
            detail: soloStatus.detail,
          },
        }).catch((err) => console.error("[SOLO] notify_solo_result failed:", err));
      }
    }
  }, [soloStatus, soloStep, soloPlan, soloConfirmation]);

  useEffect(() => {
    return () => {
      if (reconnectTimerRef.current) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      const socket = socketRef.current;
      socketRef.current = null;
      activePortRef.current = null;
      if (
        socket &&
        (socket.readyState === WebSocket.OPEN ||
          socket.readyState === WebSocket.CONNECTING)
      ) {
        socket.close();
      }
    };
  }, []);

  const sendMessage = (content: string, attachments: AttachmentRef[] = []) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setStatusLine("后端服务未就绪");
      setStatusDetail("当前连接尚未建立完成，消息未发送。");
      return false;
    }

    const now = new Date().toISOString();
    const requestId = createId("req");

    setMessages((current) => [
      ...current,
      {
        id: createId("user"),
        requestId,
        role: "user",
        content,
        attachments,
        createdAt: now,
        status: "done",
      },
      {
        id: createId("assistant"),
        requestId,
        role: "assistant",
        content: "",
        createdAt: now,
        status: "pending",
        traces: [],
      },
    ]);

    const envelope: Envelope<{ content: string; attachments?: AttachmentRef[] }> = {
      type: "client:send_message",
      requestId,
      conversationId,
      payload: { content, attachments },
      timestamp: now,
    };

    socket.send(JSON.stringify(envelope));
    setStatusLine("AI 正在思考");
    setStatusDetail("请求已发送，等待模型开始生成。");
    return true;
  };

  const pauseSolo = () => sendSoloControl({ action: "pause" });
  const resumeSolo = () => sendSoloControl({ action: "resume" });
  const stopSolo = () => sendSoloControl({ action: "stop" });
  const allowDangerousStep = () => {
    setSoloConfirmation(null);
    return sendSoloControl({ action: "confirm_allow" });
  };
  const rejectDangerousStep = () => {
    setSoloConfirmation(null);
    return sendSoloControl({ action: "confirm_reject" });
  };

  const sendToolConfirmation = (decision: "allow" | "reject") => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN || !toolConfirmation) {
      return false;
    }
    const now = new Date().toISOString();
    const envelope: Envelope<{
      confirmationId: string;
      decision: "allow" | "reject";
    }> = {
      type: "client:tool_confirmation",
      requestId: createId("tool-confirm"),
      conversationId,
      payload: {
        confirmationId: toolConfirmation.confirmationId,
        decision,
      },
      timestamp: now,
    };
    socket.send(JSON.stringify(envelope));
    setToolConfirmation(null);
    setStatusLine(decision === "allow" ? "工具确认已发送" : "工具动作已拒绝");
    setStatusDetail(null);
    return true;
  };

  const sendScheduledTaskMessage = (
    type: string,
    payload: Record<string, unknown>,
  ) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    const envelope: Envelope<Record<string, unknown>> = {
      type: type as Envelope<unknown>["type"],
      requestId: createId("scheduled"),
      conversationId,
      payload,
      timestamp: new Date().toISOString(),
    };
    socket.send(JSON.stringify(envelope));
    return true;
  };

  const requestScheduledTasks = () =>
    sendScheduledTaskMessage("client:scheduled_task_list", {});
  const createScheduledTask = (task: Omit<ScheduledTask, "id" | "createdAt" | "updatedAt">) =>
    sendScheduledTaskMessage("client:scheduled_task_create", { task });
  const updateScheduledTask = (task: ScheduledTask) =>
    sendScheduledTaskMessage("client:scheduled_task_update", { task });
  const deleteScheduledTask = (taskId: string) =>
    sendScheduledTaskMessage("client:scheduled_task_delete", { taskId });
  const requestScheduledTaskHistory = (taskId: string) =>
    sendScheduledTaskMessage("client:scheduled_task_history", { taskId });

  const saveMemoryState = (memory: MemoryState) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setStatusLine("后端服务未就绪");
      setStatusDetail("当前连接尚未建立完成，记忆未保存。");
      return false;
    }
    const envelope: Envelope<MemoryState> = {
      type: "client:memory_save",
      requestId: createId("memory-save"),
      conversationId,
      payload: memory,
      timestamp: new Date().toISOString(),
    };
    socket.send(JSON.stringify(envelope));
    return true;
  };

  return {
    backend,
    messages,
    canSend: backend.phase === "connected",
    sendMessage,
    statusDetail,
    statusLine,
    soloStatus,
    soloStep,
    soloConfirmation,
    toolConfirmation,
    soloDisplays,
    soloTimeline,
    soloLastError,
    soloPlan,
    imStatuses,
    wechatBindStatus,
    requestSoloDisplays,
    startWechatBind: (force = false) =>
      sendWechatControl("client:wechat_bind_start", { force }),
    cancelWechatBind: () => sendWechatControl("client:wechat_bind_cancel"),
    unbindWechat: () => sendWechatControl("client:wechat_unbind"),
    pauseSolo,
    resumeSolo,
    stopSolo,
    allowDangerousStep,
    rejectDangerousStep,
    allowToolConfirmation: () => sendToolConfirmation("allow"),
    rejectToolConfirmation: () => sendToolConfirmation("reject"),
    overlayVisible,
    setOverlayVisible,
    scheduledTasks,
    scheduledTaskHistory,
    memoryState,
    requestMemoryState,
    saveMemoryState,
    requestScheduledTasks,
    createScheduledTask,
    updateScheduledTask,
    deleteScheduledTask,
    requestScheduledTaskHistory,
  };
}
