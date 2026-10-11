import { randomUUID } from 'node:crypto';
import { AgentRuntime, CapabilityRegistry, type TaskHandle } from './runtime.js';
import type { AgentEngine, AuthorizationRequest, Authorizer } from './contracts.js';

// Selective adaptation of the existing protocol envelope and request-bound
// confirmation broker. No BackendApplication/settings/service composition root.
export interface SessionEnvelope {
  type: string; requestId: string; conversationId: string;
  payload: Record<string, unknown>; timestamp: string;
}
export function parseSessionEnvelope(value: unknown): SessionEnvelope {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid envelope');
  const e = value as SessionEnvelope;
  if (typeof e.type !== 'string' || !/^[a-zA-Z0-9:_-]{1,100}$/.test(e.type)
    || typeof e.requestId !== 'string' || !/^[a-zA-Z0-9_-]{1,150}$/.test(e.requestId)
    || typeof e.conversationId !== 'string' || !/^[a-zA-Z0-9_-]{1,150}$/.test(e.conversationId)
    || typeof e.timestamp !== 'string' || !Number.isFinite(Date.parse(e.timestamp))
    || !e.payload || typeof e.payload !== 'object' || Array.isArray(e.payload)
    || JSON.stringify(e).length > 128_000) throw new Error('invalid envelope');
  return structuredClone(e);
}
export interface SessionExecution {
  workspace: string; registry: CapabilityRegistry; engine: AgentEngine;
  cleanup(): Promise<void>;
  authorize?: Authorizer;
  report?(): Record<string,unknown>;
}
type Entry = { input: string; handle?: TaskHandle; finished: Promise<void>; cancelRequested: boolean };
type Pending = { conversationId: string; requestId: string; finish(allow: boolean): void };

export class FixtureSession {
  private readonly entries = new Map<string, Entry>();
  private readonly active = new Map<string, string>();
  private readonly confirmations = new Map<string, Pending>();
  private closed = false;
  constructor(private readonly options: {
    send(envelope: SessionEnvelope): void;
    create(envelope: SessionEnvelope, onText:(text:string)=>void): Promise<SessionExecution>;
    mode?: "fixture" | "pi" | "gui";
    maxRequests?: number;
  }) {}
  private emit(e: SessionEnvelope, type: string, payload: Record<string, unknown>) {
    if (!this.closed) this.options.send({ type, payload, requestId: e.requestId, conversationId: e.conversationId, timestamp: new Date().toISOString() });
  }
  receive(value: unknown): void {
    if (this.closed) throw new Error('session closed');
    const e = parseSessionEnvelope(value);
    const key = JSON.stringify([e.conversationId, e.requestId]);
    if (e.type === 'client:tool_confirmation') {
      const p = this.confirmations.get(String(e.payload.confirmationId));
      if (p && p.conversationId === e.conversationId && (e.payload.decision === 'allow' || e.payload.decision === 'reject')) p.finish(e.payload.decision === 'allow');
      return;
    }
    if (e.type === 'client:cancel_task') {
      const target = this.entries.get(JSON.stringify([e.conversationId, e.payload.targetRequestId]));
      if (target) { target.cancelRequested = true; target.handle?.cancel(); }
      return;
    }
    // Existing UI initialization requests never initialize services or save secrets.
    if (['client:memory_get','client:update_settings','client:token_usage_get','client:scheduled_tasks_get'].includes(e.type)) return;
    if (e.type !== 'client:send_message') { this.emit(e, 'server:error', { message: 'Fixture mode does not support this operation' }); return; }
    if (typeof e.payload.content !== 'string' || !e.payload.content.trim() || e.payload.content.length > 16_000) throw new Error('invalid content');
    const input = JSON.stringify(e.payload);
    const existing = this.entries.get(key);
    if (existing) {
      if (existing.input !== input) this.emit(e,'server:error',{ message:'request ID reused with different input' });
      return; // retries never execute or produce another terminal event
    }
    if (this.active.has(e.conversationId)) { this.emit(e,'server:error',{ message:'conversation already has an active fixture task' }); return; }
    if (this.entries.size >= (this.options.maxRequests ?? 128)) throw new Error('fixture session capacity reached; reopen session');
    const entry: Entry = { input, finished: Promise.resolve(), cancelRequested: false };
    this.entries.set(key, entry); this.active.set(e.conversationId, key);
    entry.finished = this.run(e, entry).finally(() => this.active.delete(e.conversationId));
    // Transport errors must not leave unobserved task promises.
    void entry.finished.catch(() => { entry.handle?.cancel(); });
  }
  private authorize(e: SessionEnvelope, request: Readonly<AuthorizationRequest>, signal: AbortSignal): Promise<boolean> {
    const id = `confirm-${randomUUID()}`;
    return new Promise<boolean>((resolve) => {
      let settled = false;
      const finish = (allow: boolean) => {
        if (settled) return; settled = true;
        clearTimeout(timer); signal.removeEventListener('abort', abort); this.confirmations.delete(id); resolve(allow);
      };
      const abort = () => finish(false);
      const timer = setTimeout(abort, Math.max(1, request.expiresAt - Date.now()));
      this.confirmations.set(id, { conversationId: e.conversationId, requestId: e.requestId, finish });
      if (signal.aborted || this.closed) { finish(false); return; }
      signal.addEventListener('abort', abort, { once: true });
      try { this.emit(e, 'server:tool_confirmation_required', { confirmation: {
        confirmationId: id, riskLevel: 'confirm', kind: 'tool', name: request.capability,
        reason: 'TEST MODE: approve one simulated desktop action; no real desktop input.',
        params: request.args, createdAt: new Date().toISOString(),
      } }); } catch { finish(false); }
    });
  }
  private async run(e: SessionEnvelope, entry: Entry): Promise<void> {
    let execution: SessionExecution | undefined;
    let runtimeOwnsCleanup = false;
    let cleanupStarted = false;
    const cleanup = async () => {
      if (cleanupStarted || !execution) return;
      cleanupStarted = true;
      await execution.cleanup();
    };
    try {
      this.emit(e, 'server:status', { stage: 'thinking', fixture: this.options.mode !== 'pi' && this.options.mode !== 'gui' });
      this.emit(e, 'server:agent_progress', { content: (this.options.mode === 'gui'||e.payload.content==='GUI_REHEARSAL_FOREGROUND') ? '真实 GUI 前台验收：仅专用窗口，失焦停止；脚本规划，非模型自主。' : this.options.mode === 'pi' ? 'Pi 实验会话：仅临时示例只读工具；费用未知。' : 'TEST MODE · scripted engine / fake desktop broker' });
      execution = await this.options.create(e,text=>{if(text&&!entry.cancelRequested)this.emit(e,'server:message_delta',{content:text});});
      if (entry.cancelRequested || this.closed) {
        await cleanup(); execution = undefined;
        this.emit(e,'server:message',{ content:'Fixture cancelled; cleanup complete.', route:'answer_directly', status:'cancelled', fixture:this.options.mode !== 'pi' && this.options.mode !== 'gui' });
        return;
      }
      const runtime = new AgentRuntime({ registry: execution.registry, cleanup,
        authorize: execution.authorize ?? ((request, signal) => this.authorize(e, request, signal)),
        onRecord: record => this.emit(e,'server:trace',{ trace: { id: record.id, kind:'tool', name:record.capability,
          status: record.status === 'succeeded' ? 'completed' : 'error', summary:record.status,
          startedAt:record.startedAt, completedAt:record.finishedAt, result:JSON.stringify(record.output?.value ?? record.error ?? record.status) } }),
      });
      entry.handle = runtime.start({ workspace:execution.workspace, goal:e.payload.content as string },execution.engine);
      runtimeOwnsCleanup = true;
      if (entry.cancelRequested || this.closed) entry.handle.cancel();
      const result = await entry.handle.result;
      this.emit(e,'server:message',{ ...(execution.report ? {modelStats:execution.report()} : {}), content:result.status === 'completed' ? result.answer : result.status === 'failed' ? (this.options.mode === 'pi' ? `Pi 请求失败：${safeModelError(result.error)}` : `Fixture failed: ${result.error ?? 'unknown failure'}`) : `Fixture ${result.status}; cleanup complete.`, route:'answer_directly', status:result.status, fixture:this.options.mode !== 'pi' && this.options.mode !== 'gui' });
    } catch (error) {
      if (execution && !runtimeOwnsCleanup) await cleanup().catch(() => {});
      this.emit(e,'server:message',{ content:this.options.mode === 'pi' ? `Pi 请求失败：${safeModelError(error)}` : `Fixture failed: ${String(error)}`,route:'answer_directly',status:'failed',fixture:this.options.mode !== 'pi' && this.options.mode !== 'gui' });
    }
  }
  async idle(): Promise<void> { await Promise.allSettled([...this.entries.values()].map(e => e.finished)); }
  async close(): Promise<void> {
    this.closed = true;
    for (const entry of this.entries.values()) { entry.cancelRequested = true; entry.handle?.cancel(); }
    for (const pending of [...this.confirmations.values()]) pending.finish(false);
    await this.idle();
  }
}

function safeModelError(error:unknown):string {
  const text=String(error);
  // Allowlisted host guidance only: provider text may contain credentials or target content.
  const uiaGuidance:Record<string,string>={
    UIA_REVOKED:'UIA 授权已撤销；需要使用时请重新明确授权。',
    UIA_TARGET_CHANGED:'目标控件已变化；请重新观察并确认目标。',
    UIA_WINDOW_CHANGED:'目标窗口已关闭或变化；请重新选择并授权目标。',
    UIA_PROCESS_CHANGED:'目标进程已变化；请重新选择并授权目标。',
    UIA_TARGET_REUSED:'目标身份已变化；请重新选择并授权目标。',
    UIA_BINDING_REJECTED:'无法确认目标身份；请检查宿主目标清单。',
    UIA_LAUNCH_FAILED:'UIA 驱动无法启动；请检查 sidecar 和 .NET 10 Desktop Runtime。',
    UIA_FOCUS_CHANGED:'目标焦点发生变化；请检查目标状态，勿直接重试。',
    UIA_STALE_OBSERVATION:'观察已过期；请重新观察后再确认操作。',
    UIA_REPLAY_OR_STALE:'操作已使用或观察过期；请先检查目标状态，勿重复提交。',
    UIA_SESSION_STOPPED:'UIA 会话已停止；请先检查目标状态，勿直接重试。',
    UIA_TIMEOUT:'UIA 调用超时；目标可能已改变，请先检查，勿直接重试。',
    UIA_CANCELLED:'UIA 调用已取消；目标可能已改变，请先检查，勿直接重试。',
    UIA_PROVIDER_ERROR:'UIA provider 调用失败；目标可能已改变，请先检查，勿直接重试。',
    UIA_PROTOCOL_ERROR:'UIA 驱动回复无效；目标可能已改变，请先检查，勿直接重试。',
    UIA_CLEANUP_INCOMPLETE:'UIA 清理未确认完成；勿重试或启动其他桌面操作，请检查自有目标。',
  };
  const uiaCode=Object.keys(uiaGuidance).find(code=>new RegExp('(?:^|[^A-Z0-9_])'+code+'(?:$|[^A-Z0-9_])').test(text));
  if(uiaCode)return uiaCode+'：'+uiaGuidance[uiaCode];

  return ['MODEL_SESSION_NOT_CONFIGURED','MODEL_SESSION_BUSY','MODEL_CALL_BUDGET_EXHAUSTED','MODEL_INPUT_LIMIT','MODEL_OUTPUT_LIMIT','MODEL_INCOMPLETE_STREAM','MODEL_TIMEOUT','MODEL_CANCELLED','MODEL_PROVIDER_FAILED'].find(code=>text.includes(code)) ?? (['GUI_INITIAL_TARGET_UNAVAILABLE','GUI_INITIAL_ACTIVATION_REJECTED','GUI_NATIVE_HANDLE_MISMATCH','GUI_FOCUS_LOST','GUI_APPROVAL_TIMEOUT','GUI_DESKTOP_BUSY','GUI_CANCELLED'].find(code=>text.includes(code))??'MODEL_RUNTIME_FAILED');
}
