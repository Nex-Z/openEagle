import test from 'node:test';
import assert from 'node:assert/strict';
import {ModelSessions} from '../src/model-sessions.js';
import {offlineModel} from '../src/offline-model.js';
import {createGuiExecution} from '../src/gui-execution.js';
import {AgentRuntime} from '../src/runtime.js';
import type {CapabilityTransport} from '../src/reused-desktop-client.js';
import type {SessionEnvelope} from '../src/fixture-session.js';
const envelope:SessionEnvelope={type:'client:send_message',requestId:'gui',conversationId:'c',timestamp:new Date().toISOString(),payload:{content:'GUI_REHEARSAL_FOREGROUND'}};
test('Pi 显式 GUI 扩展注册同一通用工具；假 broker 仅验证接线，不是 OS 验收',async()=>{
  const listeners=new Set<(v:unknown)=>void>();const calls:string[]=[];let closed=false;
  const transport:CapabilityTransport={subscribe:f=>{listeners.add(f);return()=>{listeners.delete(f);};},send:raw=>{
    const r=raw as {id:string;capability:string;payload:Record<string,unknown>};calls.push(r.capability+':'+String(r.payload.operation??''));
    const result=r.payload.operation==='observe'?{target:'owned',observationId:'observation',sha256:'fake-unit-only',state:{controls:{entry:{x:1,y:2},save:{x:3,y:4}}}}:r.payload.operation==='inspect'?{fingerprint:'stable'}:{verified:true};
    queueMicrotask(()=>{for(const f of listeners)f({kind:'capability:result',id:r.id,ok:true,result});});
  }};
  const models=new ModelSessions(offlineModel);models.configure('c',{provider:'openai-like',api:'openai-completions',endpoint:'https://fixture.invalid/v1',modelId:'offline-test',maxCalls:8,maxOutputTokens:256,timeoutMs:1000,maxRetries:0,workspace:'temporary-sample',approved:true},'dummy-gui-unit-key');
  const execution=await models.create(envelope,()=>{},()=>createGuiExecution(envelope,transport,async operation=>{if(operation==='close')closed=true;return operation==='open'?{target:'owned'}:operation==='authorize'?{ticket:'unit-ticket'}:{};}));
  assert.ok(execution.registry.get('workspace_read'));for(const name of ['desktop_observe','desktop_click','desktop_type'])assert.ok(execution.registry.get(name));
  const schema=JSON.stringify(execution.registry.get('desktop_click')!.inputSchema);assert.ok(!schema.includes('entry'));assert.ok(!schema.includes('save'));assert.ok(schema.includes('target'));assert.ok(schema.includes('observationId'));
  try{const runtime=new AgentRuntime({registry:execution.registry,authorize:execution.authorize,cleanup:execution.cleanup});const result=await runtime.start({workspace:execution.workspace,goal:'GUI_REHEARSAL_FOREGROUND'},execution.engine).result;assert.equal(result.status,'completed',result.error);assert.equal(result.records.length,7);assert.ok(result.records.every(r=>r.status==='succeeded'));assert.equal(execution.report!().calls,8);assert.equal((execution.report!().steps as unknown[]).length,7);assert.equal(calls.filter(c=>c==='desktop.mouse:').length,2);assert.equal(calls.filter(c=>c==='desktop.keyboard:').length,1);assert.equal(closed,true);}finally{await execution.cleanup();models.close();}
});
test('后台限定 GUI 不启动 broker，也不注册前台工具',async()=>{let called=false;const e=await createGuiExecution({...envelope,payload:{content:'GUI_REHEARSAL_BACKGROUND'}},{send:()=>{called=true;},subscribe:()=>()=>{}},async()=>{called=true;return {};});try{assert.equal(e.registry.list().length,0);assert.equal(e.report!().routing,'needsForeground');assert.equal(called,false);}finally{await e.cleanup();}});
