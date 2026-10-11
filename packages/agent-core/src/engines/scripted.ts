import type { AgentEngine, EngineSession } from '../contracts.js';
/** 仅用于可复现测试/演示；不是模型智能或真实模型验收。 */
export class ScriptedEngine implements AgentEngine {
  readonly name = 'scripted-test-engine';
  constructor(private readonly script: (session: EngineSession) => Promise<string>) {}
  run(session: EngineSession): Promise<string> { return this.script(session); }
}
