import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AgentExecutionTrace,
  AssistantMessageBlock,
  AppSettings,
  BackendState,
  ChatMessage,
  Envelope,
  ErrorPayload,
  SoloConfirmationPayload,
  SoloDisplayOption,
  SoloControlPayload,
  SoloStatusPayload,
  SoloStepPayload,
  StatusPayload,
} from "../types/protocol";

const BACKEND_EVENT = "backend://status";
const CONNECT_RETRY_LIMIT = 8;

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function createId(prefix: string) {
  return `${prefix}-${crypto.randomUUID()}`;
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
  maxSteps: 25,
};

export function useBackendConnection(
  conversationId: string,
  settings: AppSettings,
  initialMessages: ChatMessage[],
  onMessagesChange: (conversationId: string, messages: ChatMessage[]) => void,
) {
  const [backend, setBackend] = useState<BackendState>(initialState);
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [statusLine, setStatusLine] = useState("后端服务启动中...");
  const [statusDetail, setStatusDetail] = useState<string | null>(null);
  const [soloStatus, setSoloStatus] = useState<SoloStatusPayload>(idleSoloStatus);
  const [soloStep, setSoloStep] = useState<SoloStepPayload | null>(null);
  const [soloConfirmation, setSoloConfirmation] =
    useState<SoloConfirmationPayload | null>(null);
  const [soloDisplays, setSoloDisplays] = useState<SoloDisplayOption[]>([]);
  const [soloTimeline, setSoloTimeline] = useState<string[]>([]);
  const [soloLastError, setSoloLastError] = useState<string | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const retryCountRef = useRef(0);
  const activePortRef = useRef<number | null>(null);
  const onMessagesChangeRef = useRef(onMessagesChange);
  const skipNextMessageSyncRef = useRef(true);
  const activeSoloRequestIdRef = useRef<string | null>(null);

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

  useEffect(() => {
    skipNextMessageSyncRef.current = true;
    setMessages(initialMessages);
  }, [conversationId]);

  useEffect(() => {
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
          detail?: string;
          trace?: AgentExecutionTrace;
          status?: SoloStatusPayload;
          step?: SoloStepPayload;
          confirmation?: SoloConfirmationPayload;
          displays?: SoloDisplayOption[];
          preferredDisplayIndex?: number;
        } & ErrorPayload &
          StatusPayload
      >;

      if (envelope.type === "server:message") {
        setMessages((current) =>
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
        setMessages((current) =>
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
          setMessages((current) =>
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
          setMessages((current) =>
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
          appendSoloMessage({
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
          setMessages((current) =>
            upsertAssistantTrace(current, envelope.requestId, trace),
          );
        }
        return;
      }

      if (envelope.type === "server:solo_displays") {
        setSoloDisplays(
          Array.isArray(envelope.payload.displays) ? envelope.payload.displays : [],
        );
        return;
      }

      if (envelope.type === "server:solo_status" && envelope.payload.status) {
        activeSoloRequestIdRef.current = envelope.requestId;
        const nextStatus = envelope.payload.status;
        setSoloStatus(nextStatus);
        appendSoloTimeline(
          `状态更新: ${nextStatus.state}${nextStatus.detail ? ` · ${nextStatus.detail}` : ""}`,
        );
        if (
          nextStatus.detail &&
          (nextStatus.state === "paused" ||
            nextStatus.state === "completed" ||
            nextStatus.state === "aborted" ||
            nextStatus.state === "error")
        ) {
          appendSoloMessage(
            createChatMessage({
              role: nextStatus.state === "error" ? "system" : "assistant",
              label: `SOLO ${nextStatus.state}`,
              content: nextStatus.detail,
              createdAt: new Date().toISOString(),
              requestId: envelope.requestId,
              mode: "solo",
              status: nextStatus.state === "error" ? "error" : "done",
            }),
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
        activeSoloRequestIdRef.current = envelope.requestId;
        setSoloStep(step);
        appendSoloTimeline(
          `第 ${step.stepIndex} 步: ${step.action} · ${step.thoughtSummary}`,
        );
        appendSoloMessage(
          createChatMessage({
            role: "assistant",
            label: `第 ${step.stepIndex} 步`,
            content: `${step.thoughtSummary}\n\n计划动作: \`${step.action}\`\n\n预期结果: ${step.expectedOutcome ?? "未提供"}`,
            createdAt: step.timestamp,
            requestId: envelope.requestId,
            mode: "solo",
            status: "done",
          }),
        );
        appendSoloMessage({
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
          appendSoloMessage(
            createChatMessage({
              role: "tool",
              label: "截图预览",
              content: `当前截图已获取，等待执行动作 \`${step.action}\`。`,
              createdAt: step.timestamp,
              requestId: envelope.requestId,
              mode: "solo",
              imagePath: step.screenshotPath,
              status: "done",
            }),
          );
        }
        return;
      }

      if (
        envelope.type === "server:solo_confirmation_required" &&
        envelope.payload.confirmation
      ) {
        activeSoloRequestIdRef.current = envelope.requestId;
        setSoloConfirmation(envelope.payload.confirmation);
        setSoloStatus((current) => ({
          ...current,
          state: "waiting_user_confirmation",
        }));
        appendSoloTimeline(
          `等待确认: ${envelope.payload.confirmation.action} · ${envelope.payload.confirmation.reason}`,
        );
        appendSoloMessage(
          createChatMessage({
            role: "system",
            label: "危险动作确认",
            content: `${envelope.payload.confirmation.thoughtSummary}\n\n动作: \`${envelope.payload.confirmation.action}\`\n\n原因: ${envelope.payload.confirmation.reason}`,
            createdAt: new Date().toISOString(),
            requestId: envelope.requestId,
            mode: "solo",
            status: "error",
          }),
        );
        return;
      }

      if (envelope.type === "server:error") {
        setStatusLine("后端服务异常");
        setStatusDetail(
          envelope.payload.detail ?? envelope.payload.message ?? "未知错误",
        );
        setMessages((current) => {
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

  const sendMessage = (content: string) => {
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
        createdAt: now,
        status: "done",
        mode: "chat",
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

    const envelope: Envelope<{ content: string }> = {
      type: "client:send_message",
      requestId,
      conversationId,
      payload: { content },
      timestamp: now,
    };

    socket.send(JSON.stringify(envelope));
    setStatusLine("AI 正在思考");
    setStatusDetail("请求已发送，等待模型开始生成。");
    return true;
  };

  const canStartSolo =
    backend.phase === "connected" &&
    Boolean(settings.agent.vlModelId.trim()) &&
    Boolean(settings.agent.vlApiKey.trim());

  const startSolo = async (content: string) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setStatusLine("后端服务未就绪");
      setStatusDetail("当前连接尚未建立完成，SOLO 未启动。");
      return false;
    }
    if (!settings.agent.vlModelId.trim() || !settings.agent.vlApiKey.trim()) {
      setStatusLine("SOLO 配置缺失");
      setStatusDetail("请先在设置中配置 VL 模型 ID 与 API Key。");
      return false;
    }

    const now = new Date().toISOString();
    const requestId = createId("solo");
    activeSoloRequestIdRef.current = requestId;
    setSoloStatus({
      state: "running",
      detail: "SOLO 启动中，准备首帧截图。",
      stepCount: 0,
      maxSteps: 25,
      startedAt: now,
    });
    setSoloStep(null);
    setSoloConfirmation(null);
    setSoloTimeline([]);
    setSoloLastError(null);
    appendSoloTimeline("SOLO 启动，请求已发送，等待后端截图与决策");

    setMessages((current) => [
      ...current,
      {
        id: createId("user"),
        requestId,
        role: "user",
        content,
        createdAt: now,
        status: "done",
        mode: "solo",
      },
      {
        id: createId("assistant"),
        requestId,
        role: "assistant",
        label: "SOLO",
        content: "SOLO 已启动，准备执行视觉操作。",
        createdAt: now,
        status: "pending",
        mode: "solo",
        traces: [],
      },
    ]);

    const envelope: Envelope<{
      content: string;
    }> = {
      type: "client:start_solo",
      requestId,
      conversationId,
      payload: {
        content,
      },
      timestamp: now,
    };

    socket.send(JSON.stringify(envelope));
    setStatusLine("SOLO 执行中");
    setStatusDetail("视觉操作链路已启动。");
    appendSoloTimeline("等待后端完成首帧截图并返回 VL 决策");
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
    soloDisplays,
    soloTimeline,
    soloLastError,
    canStartSolo,
    startSolo,
    requestSoloDisplays,
    pauseSolo,
    resumeSolo,
    stopSolo,
    allowDangerousStep,
    rejectDangerousStep,
  };
}
