import path from 'node:path';
import { performance } from 'node:perf_hooks';
import type { MetricEvent, MetricInput, Phase } from './metrics.js';
import { createHash, randomUUID } from 'node:crypto';
import { realpath } from 'node:fs/promises';
import type { ActionRecord, AgentEngine, Authorizer, Capability, Json, PreparedAction, TaskContext } from './contracts.js';

function frozen<T>(value: T): T {
  if (value && typeof value === 'object') {
    Object.freeze(value);
    for (const child of Object.values(value)) frozen(child);
  }
  return value;
}
function snapshot<T>(value: T): T { return frozen(structuredClone(value)); }
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k,v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`;
  return JSON.stringify(value);
}
function digest(capability: string, args: Json, action: PreparedAction): string {
  return createHash('sha256').update(canonical({capability, args, target: action.target, effects: action.effects, resources: action.resources, fingerprint: action.fingerprint ?? null})).digest('hex');
}

/** 只用于无副作用的模型/批准等待；执行器必须自行结束并清理，不能 race 后释放锁。 */
async function abortable<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  if(signal.aborted){void work.catch(()=>{});signal.throwIfAborted();}
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, {once:true});
    work.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort)).catch(() => {});
  });
}

/** 进程内共享锁；跨进程宿主必须另外提供唯一执行所有者。 */
function resourcesOverlap(a: string, b: string): boolean {
  if (a === b) return true;
  if (!a.startsWith('workspace:') || !b.startsWith('workspace:')) return false;
  const left = a.slice(10), right = b.slice(10);
  const windows = /^[a-z]:[\\/]/i.test(left) && /^[a-z]:[\\/]/i.test(right);
  const paths = windows ? path.win32 : path.posix;
  if (!paths.isAbsolute(left) || !paths.isAbsolute(right)) return false;
  const within = (parent: string, child: string) => {
    const relative = paths.relative(parent, child);
    return relative === '' || (relative !== '..' && !relative.startsWith(`..${paths.sep}`) && !paths.isAbsolute(relative));
  };
  return within(left, right) || within(right, left);
}

export class ResourceLocks {
  private held = new Set<string>();
  private waiters = new Set<() => void>();
  async acquire(resources: string[], signal: AbortSignal): Promise<() => void> {
    const keys = [...new Set(resources)].sort();
    while (true) {
      signal.throwIfAborted();
      if (keys.every(key => [...this.held].every(held => !resourcesOverlap(key, held)))) {
        keys.forEach(key => this.held.add(key));
        let released = false;
        return () => {
          if (released) return;
          released = true;
          keys.forEach(key => this.held.delete(key));
          for (const wake of [...this.waiters]) wake();
        };
      }
      await new Promise<void>((resolve, reject) => {
        const cleanup = () => { this.waiters.delete(wake); signal.removeEventListener('abort', abort); };
        const wake = () => { cleanup(); resolve(); };
        const abort = () => { cleanup(); reject(signal.reason); };
        this.waiters.add(wake);
        signal.addEventListener('abort', abort, {once:true});
      });
    }
  }
}
const sharedLocks = new ResourceLocks();
export class CapabilityRegistry {
  private capabilities = new Map<string, Capability>();
  register(capability: Capability): this {
    if (!/^[a-z][a-z0-9_]*$/.test(capability.name)) throw new Error('能力名称必须使用小写字母、数字、下划线');
    if (this.capabilities.has(capability.name)) throw new Error(`能力重复: ${capability.name}`);
    this.capabilities.set(capability.name, capability);
    return this;
  }
  get(name: string): Capability | undefined { return this.capabilities.get(name); }
  list(): Capability[] { return [...this.capabilities.values()]; }
}
export interface RuntimeOptions {
  registry: CapabilityRegistry;
  onMetric?: (event: MetricEvent) => void;
  /** 每任务宿主管理的 adapter 清理；等待完成后才交付结果。 */
  cleanup?: () => Promise<void>;
  authorize?: Authorizer;
  locks?: ResourceLocks;
  actionTimeoutMs?: number;
  taskTimeoutMs?: number;
  maxActions?: number;
  onRecord?: (record: Readonly<ActionRecord>) => void;
}
export interface TaskResult { taskId: string; engine: string; status: 'completed' | 'failed' | 'cancelled' | 'timed-out'; answer: string; records: readonly ActionRecord[]; error?: string }
export interface TaskHandle { id: string; result: Promise<TaskResult>; cancel(): void }

export class AgentRuntime {
  private cleanupTaskStarted = false;
  constructor(private readonly options: RuntimeOptions) {
    for (const value of [options.actionTimeoutMs ?? 30_000, options.taskTimeoutMs ?? 120_000, options.maxActions ?? 40]) {
      if (!Number.isSafeInteger(value) || value <= 0 || value > 2_147_483_647) throw new Error('超时和动作预算必须是有效正整数');
    }
  }
  private metric(taskId:string, event:MetricInput):void { try { this.options.onMetric?.({...event,taskId,monotonicMs:performance.now()}); } catch { /* telemetry never changes execution */ } }
  private async span<T>(taskId:string,stepId:string,phase:Phase,operation:()=>Promise<T>):Promise<T> {
    const spanId=randomUUID(); this.metric(taskId,{kind:'span-start',spanId,phase,stepId,attemptId:stepId});
    try{return await operation();}finally{this.metric(taskId,{kind:'span-end',spanId,phase,stepId,attemptId:stepId});}
  }
  start(input: {goal: string; workspace: string}, engine: AgentEngine): TaskHandle {
    if(this.options.cleanup && this.cleanupTaskStarted) throw new Error('带session cleanup的runtime仅允许一个任务；请每任务创建runtime');
    this.cleanupTaskStarted = true;
    const id = randomUUID();
    const controller = new AbortController();
    return { id, cancel: () => controller.abort(new Error('用户取消任务')), result: this.run(id, input, engine, controller).then(async result => {
      try { await this.span(id,id,'cleanup',async()=>{await this.options.cleanup?.();}); return result; }
      catch (error) { return { ...result, status: 'failed' as const, error: `adapter cleanup failed: ${String(error)}` }; }
    }) };
  }
  private async run(id: string, input: {goal: string; workspace: string}, engine: AgentEngine, controller: AbortController): Promise<TaskResult> {
    const records: ActionRecord[] = [];
    let taskTimedOut = false;
    const timer = setTimeout(() => { taskTimedOut = true; controller.abort(new Error('任务超时')); }, this.options.taskTimeoutMs ?? 120_000);
    const pending = new Set<Promise<ActionRecord>>();
    let accepting = true;
    let count = 0;
    try {
      const context: TaskContext = {id, goal: input.goal, workspace: await realpath(input.workspace), facts: []};
      const sessionContext = Object.freeze({id, goal: context.goal, workspace: context.workspace, get facts() { return snapshot(context.facts); }});
      controller.signal.throwIfAborted();
      const answer = await abortable(engine.run({
        context: sessionContext,
        metric: event => this.metric(id,event),
        tools: snapshot(this.options.registry.list().filter(c => c.availability.supported).map(({name,description,inputSchema}) => ({name,description,inputSchema}))),
        signal: controller.signal,
        invoke: (name, args) => {
          if (!accepting || controller.signal.aborted) return Promise.reject(new Error('任务已停止'));
          if (++count > (this.options.maxActions ?? 40)) { controller.abort(new Error('动作预算已耗尽')); return Promise.reject(controller.signal.reason); }
          const promise = this.act(context, name, args, controller.signal).then(mutableRecord => {
            const record = snapshot(mutableRecord);
            records.push(record);
            if (record.status === 'succeeded' && record.output) context.facts.push(...snapshot(record.output.evidence));
            try { this.options.onRecord?.(snapshot(record)); } catch { /* 展示层错误不能改变动作结果。 */ }
            return record;
          });
          pending.add(promise);
          void promise.finally(() => pending.delete(promise)).catch(() => {});
          return promise;
        },
      }), controller.signal);
      accepting = false;
      await Promise.allSettled([...pending]);
      controller.signal.throwIfAborted();
      return {taskId:id, engine:engine.name, status:'completed', answer, records:snapshot(records)};
    } catch (error) {
      accepting = false;
      const cancelled = controller.signal.aborted;
      controller.abort(error);
      await Promise.allSettled([...pending]);
      return {taskId:id, engine:engine.name, status:taskTimedOut ? 'timed-out' : cancelled ? 'cancelled' : 'failed', answer:'', records:snapshot(records), error:String(error)};
    } finally { accepting = false; clearTimeout(timer); }
  }
  private async act(context: TaskContext, name: string, args: Json, taskSignal: AbortSignal): Promise<ActionRecord> {
    const controller = new AbortController();
    const signal = AbortSignal.any([taskSignal, controller.signal]);
    let timedOut = false;
    const deadline = Date.now() + (this.options.actionTimeoutMs ?? 30_000);
    const timer = setTimeout(() => { timedOut = true; controller.abort(new Error('动作超时')); }, this.options.actionTimeoutMs ?? 30_000);
    const record: ActionRecord = {id:randomUUID(), taskId:context.id, capability:name, args:null, startedAt:new Date().toISOString(), finishedAt:'', status:'failed'};
    this.metric(context.id,{kind:'tool-call',stepId:record.id,attemptId:record.id,adapter:name.split('_')[0]});
    this.metric(context.id,{kind:'handoff',stepId:record.id,attemptId:record.id,adapter:name.split('_')[0],status:'requested'});
    const timed = <T>(phase:Phase,work:()=>Promise<T>)=>this.span(context.id,record.id,phase,work);
    let release: (() => void) | undefined;
    try {
      if (JSON.stringify(args).length > 65_536) throw new Error('动作参数过大');
      record.args = snapshot(args);
      signal.throwIfAborted();
      const capability = this.options.registry.get(name);
      if (!capability) throw new Error(`未知能力: ${name}`);
      if (!capability.availability.supported) throw new Error(capability.availability.reason ?? '能力未实现');
      let prepared = await timed('prepare',()=>capability.prepare(record.args,snapshot(context),signal));
      release = await timed('lock',()=>(this.options.locks ?? sharedLocks).acquire(prepared.resources,signal));
      const locked = await timed('prepare',()=>capability.prepare(record.args,snapshot(context),signal));
      if (canonical(prepared.resources) !== canonical(locked.resources)) throw new Error('资源目标已变化，请重新观察');
      prepared = locked;
      record.target = prepared.target;
      record.effects = [...prepared.effects];
      const hash = digest(name, record.args, prepared);
      record.authorizationHash = hash;
      signal.throwIfAborted();
      if (prepared.effects.some(effect => effect !== 'read')) {
        const allowed = await timed('approval',()=>abortable(Promise.resolve(this.options.authorize?.(snapshot({actionId:record.id, taskId:context.id, capability:name, args:record.args, target:prepared.target, effects:prepared.effects, digest:hash, expiresAt:deadline}), signal)), signal));
        signal.throwIfAborted();
        if (allowed !== true) { record.status = 'denied'; return record; }
        const fresh = await timed('prepare',()=>capability.prepare(record.args,snapshot(context),signal));
        if (digest(name, record.args, fresh) !== hash) throw new Error('批准后动作目标或风险变化，请重新确认');
        prepared = fresh;
      }
      signal.throwIfAborted();
      const output = await timed('tool',()=>prepared.execute(signal));
      record.output = snapshot(output);
      signal.throwIfAborted();
      const verified = await timed('verify',()=>prepared.verify(output,signal));
      signal.throwIfAborted();
      record.status = verified ? 'succeeded' : 'unverified';
      return record;
    } catch (error) {
      record.status = timedOut ? 'timed-out' : signal.aborted ? 'cancelled' : 'failed';
      record.error = String(error);
      return record;
    } finally {
      // 不使用 Promise.race 提前释放锁；执行器必须完成取消清理后才返回。
      this.metric(context.id,{kind:'handoff',stepId:record.id,attemptId:record.id,adapter:name.split('_')[0],status:record.status});
      release?.(); clearTimeout(timer); record.finishedAt = new Date().toISOString();
    }
  }
}
