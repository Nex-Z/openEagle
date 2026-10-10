import type { BackendPortEvent, ElectronAPI } from "./electron-bridge";

export interface BackendMessageEvent {
  data: string;
}

export interface BackendSocket {
  readonly readyState: number;
  send(data: string): void;
  close(): void;
  addEventListener(type: "message", listener: (event: BackendMessageEvent) => void): void;
  addEventListener(type: "open" | "close" | "error", listener: () => void): void;
}

type SocketEventType = "open" | "close" | "error" | "message";
type SocketListener = (event: BackendMessageEvent | undefined) => void;

class ElectronBackendSocket {
  readonly #transport: NonNullable<ElectronAPI["backendPort"]>;
  readonly #listeners = new Map<SocketEventType, Set<SocketListener>>();
  readonly #unsubscribe: () => void;
  readyState: number = WebSocket.CONNECTING;

  constructor(transport: NonNullable<ElectronAPI["backendPort"]>) {
    this.#transport = transport;
    this.#unsubscribe = transport.onEvent((event) => this.#handle(event));
  }

  addEventListener(type: SocketEventType, listener: SocketListener): void {
    const listeners = this.#listeners.get(type) ?? new Set<SocketListener>();
    listeners.add(listener);
    this.#listeners.set(type, listeners);
  }

  send(data: string): void {
    if (this.readyState !== WebSocket.OPEN || !this.#transport.send(data)) {
      throw new Error("MessagePort backend is not writable");
    }
  }

  close(): void {
    if (this.readyState === WebSocket.CLOSED) return;
    this.readyState = WebSocket.CLOSED;
    this.#unsubscribe();
    this.#transport.close();
    this.#emit("close");
    this.#listeners.clear();
  }

  #handle(event: BackendPortEvent): void {
    if (this.readyState === WebSocket.CLOSED) return;
    if (event.kind === "open") {
      this.readyState = WebSocket.OPEN;
      this.#emit("open");
      return;
    }
    if (event.kind === "message") {
      this.#emit("message", { data: event.data });
      return;
    }
    if (event.kind === "error") {
      this.#emit("error");
      return;
    }
    this.readyState = WebSocket.CLOSED;
    this.#unsubscribe();
    this.#emit("close");
  }

  #emit(type: SocketEventType, event?: BackendMessageEvent): void {
    for (const listener of this.#listeners.get(type) ?? []) listener(event);
  }
}

export function createBackendSocket(port: number | null): BackendSocket {
  if (window.electronAPI?.fixtureMode) {
    const transport = window.electronAPI.backendPort;
    if (!transport) throw new Error("Fixture MessagePort unavailable");
    return new ElectronBackendSocket(transport) as unknown as BackendSocket;
  }
  return new WebSocket(`ws://127.0.0.1:${port}/ws`) as unknown as BackendSocket;
}
