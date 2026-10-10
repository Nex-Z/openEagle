import { spawnSync } from 'node:child_process';
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { AgentRuntime, CapabilityRegistry, ResourceLocks } from '../src/runtime.js';
import { ScriptedEngine } from '../src/engines/scripted.js';
import { nativeDesktopCapabilities } from '../src/adapters/native-desktop.js';
import type { Capability, Effect } from '../src/contracts.js';

function capability(options: {effect?: Effect; execute?: () => void; verify?: boolean; wait?: number; target?: () => string} = {}): Capability {
  return {name:'test_action', description:'测试能力', inputSchema:{type:'object'}, availability:{supported:true},
    async prepare() {
      return {target:options.target?.() ?? 'fixture', resources:['test:shared'], effects:[options.effect ?? 'read'],
        async execute(signal) { options.execute?.(); if (options.wait) await delay(options.wait, undefined, {signal}); return {value:'ok', evidence:[{kind:'fixture',source:'test',value:'ok'}]}; },
        async verify() { return options.verify ?? true; },
      };
    },
  };
}
async function workspace(t: test.TestContext): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(),'openeagle-runtime-'));
  t.after(() => rm(dir,{recursive:true,force:true}));
  return dir;
}
test('写入、命令、删除、外发和敏感设置均默认拒绝且没有副作用', async t => {
  const dir = await workspace(t);
  for (const effect of ['write','execute','destructive','external-submit','sensitive-setting'] as const) {
    let sideEffects = 0;
    const runtime = new AgentRuntime({registry:new CapabilityRegistry().register(capability({effect,execute:()=>sideEffects++}))});
    const result = await runtime.start({goal:'不应写入',workspace:dir},new ScriptedEngine(async session => {
      const record = await session.invoke('test_action',{approve:true, effects:['read']});
      assert.equal(record.status,'denied'); return 'done';
    })).result;
    assert.equal(result.records.length,1); assert.equal(sideEffects,0);
  }
});
test('成功必须通过独立验证，失败证据不加入上下文', async t => {
  const runtime = new AgentRuntime({registry:new CapabilityRegistry().register(capability({verify:false}))});
  const result = await runtime.start({goal:'验证',workspace:await workspace(t)},new ScriptedEngine(async session => {
    assert.equal((await session.invoke('test_action',{})).status,'unverified');
    assert.equal(session.context.facts.length,0); return '模型声称成功不改变记录';
  })).result;
  assert.equal(result.records[0]?.status,'unverified');
});
test('批准参数不可修改，目标变化后不执行，批准不跨动作复用', async t => {
  let target = 'a', executed = 0, approvals = 0;
  const runtime = new AgentRuntime({registry:new CapabilityRegistry().register(capability({effect:'write',target:()=>target,execute:()=>executed++})), authorize:async request => {
    approvals++; assert.match(request.digest,/^[0-9a-f]{64}$/);
    assert.throws(()=>{ (request.args as {value:string}).value='changed'; });
    if (approvals === 1) target='b';
    return approvals === 1;
  }});
  const result = await runtime.start({goal:'确认绑定',workspace:await workspace(t)},new ScriptedEngine(async session => {
    await session.invoke('test_action',{value:'original'}); await session.invoke('test_action',{value:'original'}); return 'done';
  })).result;
  assert.deepEqual(result.records.map(r=>r.status),['failed','denied']); assert.equal(executed,0); assert.equal(approvals,2);
});
test('共享资源跨 runtime 串行，取消等待者不执行', async t => {
  const dir = await workspace(t); let calls = 0;
  let started!: () => void, finish!: () => void;
  const ready = new Promise<void>(resolve => { started=resolve; });
  const gate = new Promise<void>(resolve => { finish=resolve; });
  const cap = capability();
  const originalPrepare=cap.prepare;
  cap.prepare=async (...args)=>{
    const prepared=await originalPrepare(...args);
    return {...prepared,execute:async signal=>{ calls++; started(); await gate; return prepared.execute(signal); }};
  };
  const registry = new CapabilityRegistry().register(cap);
  const engine = new ScriptedEngine(async s => {await s.invoke('test_action',{}); return 'done';});
  const first = new AgentRuntime({registry}).start({goal:'a',workspace:dir},engine);
  await ready;
  const second = new AgentRuntime({registry}).start({goal:'b',workspace:dir},engine);
  await delay(10); second.cancel();
  const result = await second.result;
  assert.equal(result.status,'cancelled'); assert.equal(calls,1);
  finish(); await first.result;
});
test('超时和取消等待执行器清理，之后资源可用', async t => {
  const registry = new CapabilityRegistry().register(capability({wait:1000}));
  const dir = await workspace(t);
  const result = await new AgentRuntime({registry,actionTimeoutMs:15}).start({goal:'超时',workspace:dir},new ScriptedEngine(async s => {
    assert.equal((await s.invoke('test_action',{})).status,'timed-out'); return 'done';
  })).result;
  assert.equal(result.records[0]?.status,'timed-out');
  const handle = new AgentRuntime({registry}).start({goal:'取消',workspace:dir},new ScriptedEngine(async s=>{await s.invoke('test_action',{});return 'done';}));
  await delay(10); handle.cancel(); assert.equal((await handle.result).status,'cancelled');
});
test('预算、未知能力和原生不支持状态不会伪装成功', async t => {
  const registry = new CapabilityRegistry(); nativeDesktopCapabilities().forEach(c=>registry.register(c));
  const result = await new AgentRuntime({registry,maxActions:2}).start({goal:'预算',workspace:await workspace(t)},new ScriptedEngine(async s => {
    assert.equal(s.tools.length,0);
    assert.equal((await s.invoke('windows_uia',{})).status,'failed');
    assert.equal((await s.invoke('unknown',{})).status,'failed');
    await s.invoke('unknown',{}); return 'unreachable';
  })).result;
  assert.equal(result.status,'cancelled'); assert.equal(result.records.length,2);
});
test('展示层抛错不重试副作用，结束后不能再调用工具', async t => {
  let invoke: ((name:string,args:{})=>Promise<unknown>) | undefined; let calls = 0;
  const runtime = new AgentRuntime({registry:new CapabilityRegistry().register(capability({execute:()=>calls++})),onRecord:()=>{throw new Error('UI');}});
  const result = await runtime.start({goal:'展示隔离',workspace:await workspace(t)},new ScriptedEngine(async s => { invoke=s.invoke; await s.invoke('test_action',{}); return 'done';})).result;
  assert.equal(result.records[0]?.status,'succeeded'); assert.equal(calls,1);
  await assert.rejects(invoke!('test_action',{}),/任务已停止/);
});
test('不响应取消的模型或批准等待也不会阻塞任务终止', async t => {
  const dir=await workspace(t);
  const never=new Promise<string>(()=>{});
  const modelResult=await new AgentRuntime({registry:new CapabilityRegistry(),taskTimeoutMs:15}).start({goal:'模型超时',workspace:dir},new ScriptedEngine(()=>never)).result;
  assert.equal(modelResult.status,'timed-out');
  let writes=0;
  const result=await new AgentRuntime({registry:new CapabilityRegistry().register(capability({effect:'write',execute:()=>writes++})),actionTimeoutMs:15,authorize:()=>new Promise(()=>{})}).start({goal:'批准超时',workspace:dir},new ScriptedEngine(async s=>{await s.invoke('test_action',{});return 'done';})).result;
  assert.equal(result.records[0]?.status,'timed-out'); assert.equal(writes,0);
});
test('引擎无法修改动作记录或伪造成功证据', async t => {
  const runtime=new AgentRuntime({registry:new CapabilityRegistry().register(capability({effect:'write'}))});
  const result=await runtime.start({goal:'记录防篡改',workspace:await workspace(t)},new ScriptedEngine(async s=>{
    const record=await s.invoke('test_action',{nested:{value:'original'}});
    assert.throws(()=>{record.status='succeeded';});
    assert.throws(()=>{(record.args as {nested:{value:string}}).nested.value='forged';});
    assert.equal(s.context.facts.length,0); return 'done';
  })).result;
  assert.equal(result.records[0]?.status,'denied');
});
test('准备阶段获得取消信号，超时后不进入执行', async t => {
  let executed=false;
  const cap=capability();
  cap.prepare=async (_args,_context,signal)=>{
    await delay(1000,undefined,{signal}); executed=true; throw new Error('不应到达');
  };
  const result=await new AgentRuntime({registry:new CapabilityRegistry().register(cap),actionTimeoutMs:15}).start({goal:'准备取消',workspace:await workspace(t)},new ScriptedEngine(async s=>{await s.invoke('test_action',{});return 'done';})).result;
  assert.equal(result.records[0]?.status,'timed-out'); assert.equal(executed,false);
});

test('立即取消或引擎取消后立即拒绝不会产生未处理拒绝或进程崩溃',()=>{
  const runtimeUrl=new URL('../src/runtime.js',import.meta.url).href;
  const code=`import {AgentRuntime,CapabilityRegistry} from ${JSON.stringify(runtimeUrl)};
    for(const variant of ['immediate','inside-engine']){
      let calls=0,cleaned=0;let task;
      const runtime=new AgentRuntime({registry:new CapabilityRegistry(),cleanup:async()=>{cleaned++;}});
      task=runtime.start({goal:'cancel',workspace:process.cwd()},{name:'fixture',run:async s=>{calls++;if(variant==='inside-engine')task.cancel();s.signal.throwIfAborted();return 'wrong';}});
      if(variant==='immediate')task.cancel();
      const result=await task.result;
      if(result.status!=='cancelled'||cleaned!==1||(variant==='immediate'&&calls!==0))process.exit(2);
    }
    await new Promise(r=>setTimeout(r,30));console.log('safe');`;
  const child=spawnSync(process.execPath,['--unhandled-rejections=strict','--input-type=module','-e',code],{encoding:'utf8',timeout:10000,windowsHide:true});
  assert.equal(child.status,0,child.stderr);assert.match(child.stdout,/safe/);
});

test('workspace locks serialize overlapping roots, preserve boundaries and cancel queues', async () => {
  const locks = new ResourceLocks();
  const signal = new AbortController().signal;
  const parent = 'workspace:C:\\Trusted';
  const child = 'workspace:c:\\TRUSTED\\child';
  const release = await locks.acquire([parent], signal);
  const siblingRelease = await locks.acquire(['workspace:C:\\Trusted-sibling'], signal);
  siblingRelease();
  const otherDriveRelease = await locks.acquire(['workspace:D:\\Trusted'], signal);
  otherDriveRelease();
  let acquired = false;
  const waiting = locks.acquire([child, 'other:atomic'], signal).then(done => { acquired = true; return done; });
  const cancel = new AbortController();
  const cancelled = locks.acquire([child], cancel.signal);
  const rejected = assert.rejects(cancelled, /cancel queued/);
  cancel.abort(new Error('cancel queued'));
  await rejected;
  await delay(10);
  assert.equal(acquired, false);
  // A blocked multi-resource acquisition never partially holds other resources.
  const otherRelease = await locks.acquire(['other:atomic'], signal);
  otherRelease();
  release(); release();
  const childRelease = await waiting;
  let parentAcquired = false;
  const reverse = locks.acquire([parent], signal).then(done => { parentAcquired = true; return done; });
  await delay(10);
  assert.equal(parentAcquired, false);
  childRelease();
  (await reverse)();
  (await locks.acquire([parent, child], signal))();
  const posix = await locks.acquire(['workspace:/trusted'], signal);
  (await locks.acquire(['workspace:/trusted-sibling'], signal))();
  let nested = false;
  const nestedWait = locks.acquire(['workspace:/trusted/child'], signal).then(done => { nested = true; return done; });
  await delay(10); assert.equal(nested, false);
  posix(); (await nestedWait)();
});
