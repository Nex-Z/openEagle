// Selectively reused from old Node migration desktop-capability-client.ts; original SHA256 69857c89f956ca7240a6bd2e8bb7f6ccccda9522b07247f4df685b2a6e3f7213
import { randomUUID } from "node:crypto";

export type DesktopCapability =
  | "screen.capture"
  | "screen.list"
  | "desktop.mouse"
  | "desktop.keyboard"
  | "desktop.navigation";

interface CapabilityRequest {
  kind: "capability:request";
  id: string;
  requestId: string;
  capability: DesktopCapability;
  payload: Record<string, unknown>;
  confirmationId?: string;
}

interface CapabilityResult {
  kind: "capability:result";
  id: string;
  ok: boolean;
  result?: Record<string, unknown>;
  error?: { code: string; message: string };
}

export interface CapabilityTransport {
  send(message: CapabilityRequest): void;
  subscribe(listener: (message: unknown) => void): () => void;
}

export interface CapabilityMessagePort {
  postMessage(message: unknown): void;
  on(event: "message", listener: (event: unknown) => void): unknown;
  off?(event: "message", listener: (event: unknown) => void): unknown;
  removeListener?(event: "message", listener: (event: unknown) => void): unknown;
}

export class CapabilityError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "CapabilityError";
  }
}

function isCapabilityResult(value: unknown): value is CapabilityResult {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<CapabilityResult>;
  return candidate.kind === "capability:result"
    && typeof candidate.id === "string"
    && typeof candidate.ok === "boolean";
}

export class DesktopCapabilityClient {
  readonly #transport: CapabilityTransport;
  readonly #pending = new Map<string, {
    resolve: (result: Record<string, unknown>) => void;
    reject: (error: Error) => void;
    timeout: NodeJS.Timeout;
  }>();
  readonly #unsubscribe: () => void;

  constructor(transport: CapabilityTransport) {
    this.#transport = transport;
    this.#unsubscribe = transport.subscribe((message) => this.#handleResult(message));
  }

  request(
    requestId: string,
    capability: DesktopCapability,
    payload: Record<string, unknown>,
    options: { confirmationId?: string; timeoutMs?: number; signal?: AbortSignal } = {},
  ): Promise<Record<string, unknown>> {
    const id = randomUUID();
    const timeoutMs = Math.max(100, Math.min(options.timeoutMs ?? 30_000, 120_000));
    const request: CapabilityRequest = {
      kind: "capability:request",
      id,
      requestId,
      capability,
      payload,
      ...(options.confirmationId ? { confirmationId: options.confirmationId } : {}),
    };
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.#pending.delete(id);
        options.signal?.removeEventListener("abort", abort);
        reject(new CapabilityError("CAPABILITY_TIMEOUT", `${capability} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      const abort = () => {
        const pending = this.#pending.get(id);
        if (!pending) return;
        clearTimeout(pending.timeout);
        this.#pending.delete(id);
        pending.reject(new CapabilityError("CAPABILITY_CANCELLED", `${capability} was cancelled`));
      };
      if (options.signal?.aborted) {
        clearTimeout(timeout);
        reject(new CapabilityError("CAPABILITY_CANCELLED", `${capability} was cancelled`));
        return;
      }
      options.signal?.addEventListener("abort", abort, { once: true });
      this.#pending.set(id, {
        resolve: (result) => {
          options.signal?.removeEventListener("abort", abort);
          resolve(result);
        },
        reject: (error) => {
          options.signal?.removeEventListener("abort", abort);
          reject(error);
        },
        timeout,
      });
      try {
        this.#transport.send(request);
      } catch (error) {
        abort();
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  close(): void {
    this.#unsubscribe();
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(new CapabilityError("CAPABILITY_CLIENT_CLOSED", "Capability client closed"));
    }
    this.#pending.clear();
  }

  #handleResult(message: unknown): void {
    if (!isCapabilityResult(message)) return;
    const pending = this.#pending.get(message.id);
    if (!pending) return;
    clearTimeout(pending.timeout);
    this.#pending.delete(message.id);
    if (message.ok) {
      pending.resolve(message.result ?? {});
      return;
    }
    pending.reject(new CapabilityError(
      message.error?.code ?? "CAPABILITY_FAILED",
      message.error?.message ?? "Desktop capability failed",
    ));
  }
}

