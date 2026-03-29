import { useEffect, useRef, useState } from "react";
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
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const retryCountRef = useRef(0);
  const activePortRef = useRef<number | null>(null);
  const onMessagesChangeRef = useRef(onMessagesChange);
  const skipNextMessageSyncRef = useRef(true);

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
        setMessages((current) =>
          upsertAssistantTrace(current, envelope.requestId, envelope.payload.trace!),
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

  return {
    backend,
    messages,
    canSend: backend.phase === "connected",
    sendMessage,
    statusDetail,
    statusLine,
  };
}
