import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
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

const initialState: BackendState = {
  phase: "starting",
  port: null,
  message: "正在启动本地后端...",
};

export function useBackendConnection(
  conversationId: string,
  settings: AppSettings,
) {
  const [backend, setBackend] = useState<BackendState>(initialState);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [statusLine, setStatusLine] = useState("后端启动中...");
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const retryCountRef = useRef(0);
  const activePortRef = useRef<number | null>(null);

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
    setStatusLine(`正在连接 ws://127.0.0.1:${targetPort}/ws ...`);

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
      setStatusLine("已连接到本地 Agent");
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
        setStatusLine("WebSocket 已断开，正在重试...");
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
      setStatusLine("连接失败");
    });

    socket.addEventListener("error", () => {
      setStatusLine("连接异常，等待自动重试...");
    });

    socket.addEventListener("message", (event) => {
      const envelope = JSON.parse(event.data) as Envelope<
        { content?: string; detail?: string } & ErrorPayload & StatusPayload
      >;

      if (envelope.type === "server:message") {
        setMessages((current) => [
          ...current,
          {
            id: createId("assistant"),
            role: "assistant",
            content: envelope.payload.content ?? "",
            createdAt: envelope.timestamp,
            status: "done",
          },
        ]);
        setStatusLine("Agent 已回复");
        return;
      }

      if (envelope.type === "server:status") {
        setStatusLine(
          envelope.payload.detail ?? envelope.payload.stage ?? "处理中",
        );
        return;
      }

      if (envelope.type === "server:error") {
        setStatusLine(envelope.payload.message ?? "服务异常");
        setMessages((current) => [
          ...current,
          {
            id: createId("system"),
            role: "system",
            content: envelope.payload.message ?? "未知错误",
            createdAt: envelope.timestamp,
            status: "error",
          },
        ]);
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
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        socket.close();
      }
    };
  }, []);

  useEffect(() => {
    setMessages([]);
  }, [conversationId]);

  const sendMessage = (content: string) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setStatusLine("后端尚未连接完成");
      return false;
    }

    const now = new Date().toISOString();
    const requestId = createId("req");

    setMessages((current) => [
      ...current,
      {
        id: createId("user"),
        role: "user",
        content,
        createdAt: now,
        status: "done",
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
    setStatusLine("消息已发送");
    return true;
  };

  return {
    backend,
    messages,
    canSend: backend.phase === "connected",
    sendMessage,
    statusLine,
  };
}
