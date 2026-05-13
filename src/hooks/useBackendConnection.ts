import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
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
  SoloConfirmationPayload,
  SoloDisplayOption,
  SoloControlPayload,
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

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
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

function soloStepVisibleText(step: SoloStepPayload) {
  return (
    step.agentMessage?.trim() ||
    step.thoughtSummary ||
    step.expectedOutcome ||
    "步骤已更新。"
  );
}

function collectAssistantContent(blocks?: AssistantMessageBlock[]) {
  if (!blocks || blocks.length === 0) {
    return "";
  }
  return blocks
    .filter((block) => block.kind === "text")
    .map((block) => block.content)
    .join("");
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
  const soloTimelineRef = useRef(soloTimeline);
  soloTimelineRef.current = soloTimeline;
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
    if (!isTauriRuntime()) {
      setBackend({
        phase: "error",
        port: null,
        message: "当前不在 Tauri 环境，请通过 `pnpm tauri:dev` 启动。",
      });
      setStatusLine("后端服务异常");
      setStatusDetail("当前不在 Tauri 环境，请通过 `pnpm tauri:dev` 启动。");
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
      syncSettings();
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

      if (envelope.type === "server:message") {
        patchMessages((current) =>
          upsertAssistantMessage(current, envelope.requestId, (message) => {
            const blocks = cloneAssistantBlocks(message);
            if (blocks.length === 0 && envelope.payload.content) {
              blocks.push({
                id: createId("blk"),
                kind: "text",
                content: envelope.payload.content,
                status: "done",
              });
            }

            for (const block of blocks) {
              if (block.kind === "text") {
                block.status = "done";
              }
            }

            const content = collectAssistantContent(blocks) || envelope.payload.content || "";
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
        setSoloDisplays(
          Array.isArray(envelope.payload.displays) ? envelope.payload.displays : [],
        );
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

  const buildOverlayPayload = () => ({
    title: undefined as string | undefined,
    detail: soloStatusRef.current.detail ?? undefined,
    stepText: soloStepRef.current
      ? soloStepVisibleText(soloStepRef.current)
      : undefined,
    historyText: soloTimelineRef.current.slice(-3).join("\n") || undefined,
    state: soloStatusRef.current.state,
    stepCount: soloStatusRef.current.stepCount,
    maxSteps: soloStatusRef.current.maxSteps,
  });

  useEffect(() => {
    if (!isTauriRuntime()) return;

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
    if (!isTauriRuntime()) return;
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
    if (!isTauriRuntime()) {
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
  }, [soloStatus, soloStep, soloTimeline]);

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
  };
}
