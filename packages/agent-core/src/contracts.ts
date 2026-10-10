import type { MetricInput } from './metrics.js';
/** 平台无关契约。工具输出是未可信数据，不具有授权能力。 */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Effect = 'read' | 'write' | 'execute' | 'external-submit' | 'destructive' | 'sensitive-setting';
export interface Evidence { kind: string; source: string; value: Json }
export interface ActionOutput { value: Json; evidence: Evidence[] }
export interface PreparedAction {
  target: string;
  /** 绑定授权时的文件版本或目标观察；状态变化必须重新批准。 */
  fingerprint?: string;
  resources: string[];
  effects: Effect[];
  execute(signal: AbortSignal): Promise<ActionOutput>;
  verify(output: ActionOutput, signal: AbortSignal): Promise<boolean>;
}
export interface Capability {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  availability: { supported: boolean; reason?: string };
  /** prepare 只校验/读取，不执行写入。资源键必须在不同任务间一致。 */
  prepare(args: Json, context: Readonly<TaskContext>, signal?: AbortSignal): Promise<PreparedAction>;
}
export interface TaskContext {
  id: string;
  goal: string;
  workspace: string;
  facts: Evidence[];
}
export type ActionStatus = 'succeeded' | 'denied' | 'failed' | 'cancelled' | 'timed-out' | 'unverified';
export interface ActionRecord {
  id: string; taskId: string; capability: string; args: Json;
  startedAt: string; finishedAt: string; status: ActionStatus;
  target?: string; effects?: Effect[]; authorizationHash?: string;
  output?: ActionOutput; error?: string;
}
export interface AuthorizationRequest {
  actionId: string; taskId: string; capability: string; args: Json;
  target: string; effects: readonly Effect[]; digest: string; expiresAt: number;
}
/** 此回调仅由可信宿主提供；模型和网页不能产生批准。 */
export type Authorizer = (request: Readonly<AuthorizationRequest>, signal: AbortSignal) => Promise<boolean>;
export interface EngineTool { name: string; description: string; inputSchema: Record<string, unknown> }
export interface EngineSession {
  metric?: (event: MetricInput) => void;
  context: Readonly<TaskContext>;
  tools: readonly EngineTool[];
  signal: AbortSignal;
  invoke(name: string, args: Json): Promise<ActionRecord>;
}
export interface AgentEngine {
  readonly name: string;
  run(session: EngineSession): Promise<string>;
}
