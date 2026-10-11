import { contextBridge, ipcRenderer } from "electron";

// Keep the sandboxed preload self-contained. Electron's sandbox preload cannot
// load arbitrary relative CommonJS modules, so these guards intentionally mirror
// backend-port-validation.ts without importing it at runtime.
type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

function isJsonValue(value: unknown, seen = new Set<object>()): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);
  const valid = Array.isArray(value)
    ? value.every((item) => isJsonValue(item, seen))
    : Object.entries(value as Record<string, unknown>)
      .every(([key, item]) => key.length > 0 && isJsonValue(item, seen));
  seen.delete(value);
  return valid;
}

function isValidRendererEnvelope(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const envelope = value as Record<string, unknown>;
  if (typeof envelope.type !== "string" || !envelope.type.startsWith("server:")) return false;
  if (typeof envelope.requestId !== "string" || typeof envelope.conversationId !== "string") return false;
  if (typeof envelope.timestamp !== "string" || !Number.isFinite(Date.parse(envelope.timestamp))) return false;
  if (!envelope.payload || typeof envelope.payload !== "object" || Array.isArray(envelope.payload)) return false;
  return isJsonValue(envelope.payload);
}

type InboundSequenceDisposition = "next" | "duplicate" | "gap" | "invalid";

function classifyInboundSequence(
  sequence: unknown,
  lastAccepted: number,
): InboundSequenceDisposition {
  if (!Number.isSafeInteger(sequence) || (sequence as number) <= 0) return "invalid";
  const current = sequence as number;
  if (current <= lastAccepted) return "duplicate";
  if (current !== lastAccepted + 1) return "gap";
  return "next";
}

const BACKEND_PORT_CHANNEL = "open-eagle.data";
const BACKEND_PORT_PROTOCOL_VERSION = 1;

type BackendPortEvent =
  | { kind: "open" }
  | { kind: "close"; reason?: string }
  | { kind: "error"; message: string }
  | { kind: "message"; data: string };

let backendPort: MessagePort | null = null;
let backendPortConnectionId = "";
let backendPortState: "closed" | "connecting" | "open" = "closed";
let backendPortSequence = 1;
let backendPortLastInboundSequence = 0;
const backendPortPending = new Map<number, ReturnType<typeof setTimeout>>();
const backendPortListeners = new Set<(event: BackendPortEvent) => void>();

function emitBackendPortEvent(event: BackendPortEvent): void {
  for (const listener of backendPortListeners) {
    try { listener(event); } catch { /* renderer listener was disposed */ }
  }
}

function backendFrame(kind: string, fields: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    channel: BACKEND_PORT_CHANNEL,
    protocolVersion: BACKEND_PORT_PROTOCOL_VERSION,
    connectionId: backendPortConnectionId,
    kind,
    ...fields,
  };
}

function closeBackendPort(reason: string): void {
  const port = backendPort;
  backendPort = null;
  backendPortState = "closed";
  for (const timer of backendPortPending.values()) clearTimeout(timer);
  backendPortPending.clear();
  try { port?.close(); } catch { /* already disconnected */ }
  emitBackendPortEvent({ kind: "close", reason });
}

function markBackendPortOpen(): void {
  if (!backendPort || backendPortState === "open") return;
  backendPortState = "open";
  emitBackendPortEvent({ kind: "open" });
}

ipcRenderer.on("backend://port", (event, metadata: unknown) => {
  const port = event.ports[0];
  if (!port) {
    emitBackendPortEvent({ kind: "error", message: "主进程未转交后端数据端口" });
    return;
  }
  if (backendPort) closeBackendPort("backend_port_replaced");
  const record = metadata && typeof metadata === "object" && !Array.isArray(metadata)
    ? metadata as Record<string, unknown>
    : {};
  if (record.protocolVersion !== BACKEND_PORT_PROTOCOL_VERSION || typeof record.connectionId !== "string") {
    port.close();
    emitBackendPortEvent({ kind: "error", message: "后端数据端口协议版本不兼容" });
    return;
  }
  backendPort = port;
  backendPortConnectionId = record.connectionId;
  backendPortState = "connecting";
  backendPortSequence = 1;
  backendPortLastInboundSequence = 0;
  for (const timer of backendPortPending.values()) clearTimeout(timer);
  backendPortPending.clear();
  port.onmessage = (messageEvent: MessageEvent<unknown>) => {
    const value = messageEvent.data;
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const frame = value as Record<string, unknown>;
    if (
      frame.channel !== BACKEND_PORT_CHANNEL
      || frame.protocolVersion !== BACKEND_PORT_PROTOCOL_VERSION
      || frame.connectionId !== backendPortConnectionId
    ) return;
    if (frame.kind === "hello") {
      port.postMessage(backendFrame("hello_ack"));
      markBackendPortOpen();
      return;
    }
    if (frame.kind === "hello_ack") {
      markBackendPortOpen();
      return;
    }
    if (frame.kind === "heartbeat") {
      port.postMessage(backendFrame("heartbeat_ack", { sentAt: frame.sentAt }));
      return;
    }
    if (frame.kind === "ack" && typeof frame.ack === "number") {
      const timer = backendPortPending.get(frame.ack);
      if (timer) clearTimeout(timer);
      backendPortPending.delete(frame.ack);
      return;
    }
    if (frame.kind === "envelope") {
      const disposition = classifyInboundSequence(frame.seq, backendPortLastInboundSequence);
      if (disposition === "duplicate") {
        port.postMessage(backendFrame("ack", { ack: frame.seq }));
        return;
      }
      if (disposition !== "next" || !isValidRendererEnvelope(frame.envelope)) {
        emitBackendPortEvent({
          kind: "error",
          message: disposition === "gap" ? "后端端口消息序号不连续" : "后端端口消息格式无效",
        });
        closeBackendPort(disposition === "gap" ? "sequence_gap" : "invalid_envelope");
        return;
      }
      backendPortLastInboundSequence = frame.seq as number;
      const serialized = JSON.stringify(frame.envelope);
      port.postMessage(backendFrame("ack", { ack: frame.seq }));
      emitBackendPortEvent({ kind: "message", data: serialized });
      return;
    }
    if (frame.kind === "error") {
      emitBackendPortEvent({ kind: "error", message: String(frame.message ?? frame.code ?? "后端端口错误") });
      return;
    }
    if (frame.kind === "close") closeBackendPort(String(frame.reason ?? "backend_closed"));
  };
  port.onmessageerror = () => {
    emitBackendPortEvent({ kind: "error", message: "后端端口消息无法反序列化" });
  };
  (port as EventTarget).addEventListener("close", () => {
    if (backendPort === port) closeBackendPort("backend_port_closed");
  }, { once: true });
  port.start();
  port.postMessage(backendFrame("hello"));
});

const fixtureMode = process.argv.includes("--openeagle-v2-fixture");
const piMode = process.argv.includes("--openeagle-v2-pi");
const guiMode = process.argv.includes("--openeagle-v2-gui");
const v2Mode = fixtureMode || piMode || guiMode;

const ALLOWED_INVOKE_CHANNELS = [
  "get_backend_state",
  "write_frontend_log",
  "load_conversation_index",
  "load_conversation_file",
  "save_conversation_file",
  "save_conversation_index",
  "delete_conversation_file",
  "load_solo_run_log",
  "capture_screenshot",
  "read_image_data_url",
  "perform_mouse_action",
  "perform_keyboard_action",
  "show_solo_overlay",
  "update_solo_overlay",
  "hide_solo_overlay",
  "solo_overlay_ready",
  "show_solo_target_highlight",
  "hide_solo_target_highlight",
  "notify_solo_result",
  "load_app_settings",
  "save_app_settings",
  "show_quick_assistant",
  "hide_quick_assistant",
  "configure_quick_assistant",
  "quick_assistant_ready",
  "read_active_selection",
  "capture_context_screenshot",
  "update_quick_assistant",
];

const ALLOWED_LISTEN_CHANNELS = [
  "backend://status",
  "main://focus_changed",
  "solo://overlay_state",
  "solo://overlay_control",
  "solo://user_dismissed",
  "quick://context",
  "quick://state",
  "quick://submit",
];

const ALLOWED_SEND_CHANNELS = [
  "solo:user-dismissed",
  "solo:overlay-control",
  "solo:overlay-layout",
  "quick:submit",
  "quick:dismiss",
  "quick:open-main",
  "quick:screenshot-selection",
  "quick:screenshot-cancel",
];

contextBridge.exposeInMainWorld("electronAPI", {
  invoke: (channel: string, args?: Record<string, unknown>) => {
    if (v2Mode && !["get_backend_state", "write_frontend_log", "load_conversation_index", "load_conversation_file", "save_conversation_file", "save_conversation_index", "delete_conversation_file", "load_app_settings", "save_app_settings"].includes(channel)) return Promise.reject(new Error("Operation disabled in fixture mode"));
    if (!ALLOWED_INVOKE_CHANNELS.includes(channel)) {
      return Promise.reject(new Error(`Blocked IPC invoke: ${channel}`));
    }
    // Pass args as a single object (Tauri-compatible pattern)
    return ipcRenderer.invoke(channel, args);
  },

  on: (channel: string, callback: (...args: unknown[]) => void): (() => void) => {
    if (!ALLOWED_LISTEN_CHANNELS.includes(channel)) {
      console.warn(`Blocked IPC listen: ${channel}`);
      return () => {};
    }
    const subscription = (_event: Electron.IpcRendererEvent, ...args: unknown[]) => {
      // Wrap in { payload } to match Tauri event shape for backward compatibility
      if (args.length === 1 && typeof args[0] === "object" && args[0] !== null) {
        callback({ payload: args[0] });
      } else {
        callback({ payload: args[0] });
      }
    };
    ipcRenderer.on(channel, subscription);
    return () => {
      ipcRenderer.removeListener(channel, subscription);
    };
  },

  emit: (channel: string, ...args: unknown[]) => {
    if (!ALLOWED_SEND_CHANNELS.includes(channel)) {
      console.warn(`Blocked IPC emit: ${channel}`);
      return;
    }
    ipcRenderer.send(channel, ...args);
  },

  convertFileSrc: (filePath: string) => {
    if (v2Mode) throw new Error("Local assets disabled in fixture mode");
    // Encode file path for use as asset:// protocol URL
    const normalized = filePath.replace(/\\/g, "/");
    return `asset:///${encodeURI(normalized)}`;
  },

  onCloseRequested: (callback: () => void) => {
    ipcRenderer.on("app:close-requested", () => {
      callback();
    });
    return () => {
      ipcRenderer.removeAllListeners("app:close-requested");
    };
  },

  backendPort: {
    close: () => closeBackendPort("renderer_disconnected"),
    onEvent: (callback: (event: BackendPortEvent) => void) => {
      backendPortListeners.add(callback);
      if (backendPortState === "open") queueMicrotask(() => callback({ kind: "open" }));
      return () => { backendPortListeners.delete(callback); };
    },
    send: (serializedEnvelope: string) => {
      if (!backendPort || backendPortState !== "open" || backendPortPending.size >= 64) return false;
      if (serializedEnvelope.length > 128_000) return false;
      let envelope: unknown;
      try { envelope = JSON.parse(serializedEnvelope); } catch { return false; }
      const seq = backendPortSequence;
      backendPortSequence += 1;
      backendPortPending.set(seq, setTimeout(() => {
        backendPortPending.delete(seq);
        emitBackendPortEvent({ kind: "error", message: `后端端口确认超时: ${seq}` });
        closeBackendPort("ack_timeout");
      }, 15_000));
      backendPort.postMessage(backendFrame("envelope", { seq, envelope }));
      return true;
    },
    state: () => backendPortState,
  },

  fixtureMode, v2Mode, piMode, guiMode,
  piOffline: process.argv.includes("--openeagle-v2-offline"),
  uiaSession: {
    status:()=>piMode?ipcRenderer.invoke('v2:uia:status'):Promise.resolve({enabled:false,ready:false,targets:[]}),
    grant:(payload:unknown)=>piMode?ipcRenderer.invoke('v2:uia:grant',payload):Promise.reject(new Error('UIA_DISABLED')),
    revoke:(conversationId:string)=>piMode?ipcRenderer.invoke('v2:uia:revoke',{conversationId}):Promise.reject(new Error('UIA_DISABLED')),
  },
  modelSession: {
    configure: (payload: unknown) => piMode ? ipcRenderer.invoke("v2:model:configure",payload) : Promise.reject(new Error("MODEL_MODE_DISABLED")),
    status: (conversationId: string) => piMode ? ipcRenderer.invoke("v2:model:status",{conversationId}) : Promise.resolve({configured:false}),
    clear: (conversationId: string) => piMode ? ipcRenderer.invoke("v2:model:clear",{conversationId}) : Promise.reject(new Error("MODEL_MODE_DISABLED")),
  },
  isElectron: true,
});
