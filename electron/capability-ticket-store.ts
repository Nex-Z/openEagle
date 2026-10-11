import { createHash } from "node:crypto";

export type DesktopCapability =
  | "screen.capture"
  | "screen.list"
  | "desktop.mouse"
  | "desktop.keyboard"
  | "desktop.navigation";

interface PendingConfirmation {
  requestId: string;
  capability: DesktopCapability;
  argsDigest: string;
  expiresAt: number;
  approved: boolean;
  authorityEpoch: number;
}

const MAX_TICKET_TTL_MS = 60_000;
const SPENT_TICKET_RETENTION_MS = 5 * 60_000;

export interface CapabilityTicketRequest {
  confirmationId?: string;
  requestId: string;
  capability: DesktopCapability;
  payload: Record<string, unknown>;
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
}

export function capabilityArgsDigest(
  capability: DesktopCapability,
  payload: Record<string, unknown>,
): string {
  return createHash("sha256").update(`${capability}\n${canonicalJson(payload)}`).digest("hex");
}

export class CapabilityTicketStore {
  readonly #confirmations = new Map<string, PendingConfirmation>();
  readonly #spent = new Map<string, number>();
  readonly #now: () => number;
  #authorityEpoch = 0;
  #authorityActive = false;

  constructor(now: () => number = Date.now) {
    this.#now = now;
  }

  get authorityEpoch(): number {
    return this.#authorityEpoch;
  }

  beginAuthorityEpoch(): number {
    this.revokeAll();
    this.#authorityEpoch += 1;
    this.#authorityActive = true;
    return this.#authorityEpoch;
  }

  isAuthorityEpochActive(authorityEpoch: number): boolean {
    return this.#authorityActive && authorityEpoch === this.#authorityEpoch;
  }

  revokeAuthorityEpoch(authorityEpoch: number): boolean {
    if (!this.isAuthorityEpochActive(authorityEpoch)) return false;
    this.revokeAll();
    this.#authorityActive = false;
    return true;
  }

  revokeAll(): number {
    this.#cleanup();
    const pending = [...this.#confirmations];
    for (const [id, confirmation] of pending) {
      this.#retire(id, confirmation.expiresAt);
    }
    return pending.length;
  }

  register(
    confirmationId: string,
    requestId: string,
    capability: DesktopCapability,
    payload: Record<string, unknown>,
    authorityEpoch: number,
    ttlMs = 60_000,
  ): boolean {
    this.#cleanup();
    if (!this.isAuthorityEpochActive(authorityEpoch)) return false;
    if (!confirmationId || this.#confirmations.has(confirmationId) || this.#spent.has(confirmationId)) {
      return false;
    }
    const expiresAt = this.#now() + Math.max(1_000, Math.min(ttlMs, MAX_TICKET_TTL_MS));
    this.#confirmations.set(confirmationId, {
      requestId,
      capability,
      argsDigest: capabilityArgsDigest(capability, payload),
      expiresAt,
      approved: false,
      authorityEpoch,
    });
    return true;
  }

  grant(
    confirmationId: string,
    requestId: string,
    capability: DesktopCapability,
    payload: Record<string, unknown>,
    authorityEpoch: number,
    ttlMs = 5_000,
  ): boolean {
    if (!this.register(confirmationId, requestId, capability, payload, authorityEpoch, ttlMs)) return false;
    const pending = this.#confirmations.get(confirmationId);
    if (!pending) return false;
    pending.approved = true;
    return true;
  }

  decide(confirmationId: string, approved: boolean): boolean {
    this.#cleanup();
    const pending = this.#confirmations.get(confirmationId);
    if (!pending) return false;
    if (!approved) {
      this.#retire(confirmationId, pending.expiresAt);
      return true;
    }
    pending.approved = true;
    return true;
  }

  decideBound(
    confirmationId: string,
    approved: boolean,
    capability?: DesktopCapability,
    payload?: Record<string, unknown>,
  ): boolean {
    this.#cleanup();
    const pending = this.#confirmations.get(confirmationId);
    if (!pending) return false;
    if (!approved) return this.decide(confirmationId, false);
    if (!capability || !payload) return false;
    if (
      pending.capability !== capability
      || pending.argsDigest !== capabilityArgsDigest(capability, payload)
    ) {
      return false;
    }
    return this.decide(confirmationId, true);
  }

  consume(request: CapabilityTicketRequest, authorityEpoch: number): boolean {
    this.#cleanup();
    if (!this.isAuthorityEpochActive(authorityEpoch)) return false;
    if (!request.confirmationId) return false;
    const pending = this.#confirmations.get(request.confirmationId);
    if (!pending || !pending.approved) return false;
    const matches = pending.authorityEpoch === authorityEpoch
      && pending.requestId === request.requestId
      && pending.capability === request.capability
      && pending.argsDigest === capabilityArgsDigest(request.capability, request.payload);
    this.#retire(request.confirmationId, pending.expiresAt);
    return matches;
  }

  #cleanup(): void {
    const now = this.#now();
    for (const [id, pending] of this.#confirmations) {
      if (pending.expiresAt <= now) this.#confirmations.delete(id);
    }
    for (const [id, expiresAt] of this.#spent) {
      if (expiresAt <= now) this.#spent.delete(id);
    }
  }

  #retire(id: string, expiresAt: number): void {
    this.#confirmations.delete(id);
    this.#spent.set(id, Math.max(expiresAt, this.#now() + SPENT_TICKET_RETENTION_MS));
  }
}
