import { mkdtemp, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CapabilityRegistry } from './runtime.js';
import { ScriptedEngine } from './engines/scripted.js';
import { DesktopCapabilityClient, type CapabilityTransport } from './reused-desktop-client.js';
import type { SessionEnvelope, SessionExecution } from './fixture-session.js';
import type { ActionOutput } from './contracts.js';

// Only the existing client contract is reused. This broker cannot access Electron
// screen/input APIs, launch commands, load settings, or contact a model provider.
export async function createFixtureExecution(e: SessionEnvelope, observe: { executed?(): void; cleaned?(): void } = {}): Promise<SessionExecution> {
  const workspace = await realpath(await mkdtemp(join(tmpdir(),'openeagle-ui-fixture-')));
  const listeners = new Set<(value: unknown) => void>();
  let count = 0, disposed = false;
  const transport: CapabilityTransport = {
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    send(message) {
      if (disposed || message.requestId !== e.requestId || message.capability !== 'desktop.mouse'
        || JSON.stringify(message.payload) !== JSON.stringify({ action:'fixture_click',target:'simulated-button' })) throw new Error('fake broker denied request');
      count++; observe.executed?.();
      queueMicrotask(() => { for (const listener of listeners) listener({ kind:'capability:result',id:message.id,ok:true,result:{ simulated:true, clicks:count } }); });
    },
  };
  const client = new DesktopCapabilityClient(transport);
  const registry = new CapabilityRegistry().register({
    name:'fixture_desktop_click',description:'Simulated desktop click; never real input',availability:{supported:true},inputSchema:{type:'object',properties:{},additionalProperties:false},
    async prepare(args) {
      if (!args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).length) throw new Error('fixture accepts no arguments');
      let output: ActionOutput | undefined;
      return {target:'fake-desktop:simulated-button',resources:['fake-desktop:fixture'],effects:['execute'],fingerprint:'fixture-v1',
        async execute(signal) {
          const value = await client.request(e.requestId,'desktop.mouse',{action:'fixture_click',target:'simulated-button'},{signal});
          output = {value:{simulated:true,clicks:Number(value.clicks)},evidence:[{kind:'fixture-counter',source:'fake-desktop',value:count}]};
          return output;
        },
        async verify(value,signal) { signal.throwIfAborted(); return value === output && count === 1 && !disposed; },
      };
    },
  });
  return { workspace,registry,
    engine:new ScriptedEngine(async session => {
      const record = await session.invoke('fixture_desktop_click',{});
      return record.status === 'succeeded' ? 'TEST MODE: one simulated click verified. No model or real desktop was used.' : `TEST MODE: action ${record.status}; no verified success.`;
    }),
    async cleanup() { if(disposed) return; disposed=true;client.close();listeners.clear();await rm(workspace,{recursive:true,force:true});observe.cleaned?.(); },
  };
}
