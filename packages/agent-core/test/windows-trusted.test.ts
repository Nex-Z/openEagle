import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile, readFile, symlink, mkdir, access } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createWindowsTrustedFileCapabilities, createWindowsTrustedCliCapability } from '../src/adapters/windows-trusted.js';
import { AgentRuntime, CapabilityRegistry } from '../src/runtime.js';
import { ScriptedEngine } from '../src/engines/scripted.js';
const windows = { skip: process.platform !== 'win32' };
const mode = 'trusted-workspace' as const;
async function fixture(t: test.TestContext) { const root = await mkdtemp(path.join(os.tmpdir(), 'eagle-win-')); t.after(() => rm(root, { recursive: true, force: true })); return { root, context: { id:'windows-test',goal:'fixture',workspace:root,facts:[] } }; }
const signal = () => new AbortController().signal;
const command = (code: string, extra: { timeoutMs?:number;maxOutputBytes?:number } = {}) => createWindowsTrustedCliCapability({mode,profiles:{fixture:{executable:process.execPath,validateArgs:argv=>argv.length===2 && argv[0]==='-e' && argv[1]===code}},...extra});

test('Windows opt-in 文件：真实写读hash、重复执行/参数变化/审批后变化拒绝',windows,async t=>{
  const f=await fixture(t); const [read,write]=createWindowsTrustedFileCapabilities({mode});
  await write!.prepare({path:'..notes.txt',content:'valid'},f.context);
  const args={path:'note.txt',content:'original'};
  const action=await write!.prepare(args,f.context); args.content='changed';
  const result=await action.execute(signal()); assert.equal(await action.verify(result,signal()),true);
  assert.equal(await readFile(path.join(f.root,'note.txt'),'utf8'),'original');
  await assert.rejects(action.execute(signal()),/重放/);
  const r=await read!.prepare({path:'note.txt'},f.context); const out=await r.execute(signal()); assert.equal(await r.verify(out,signal()),true);
  const overwrite=await write!.prepare({path:'note.txt',content:'replacement'},f.context);
  assert(overwrite.effects.includes('destructive'));
  await writeFile(path.join(f.root,'note.txt'),'concurrent'); await assert.rejects(overwrite.execute(signal()),/变化/);
});
test('Windows 路径：越界、ADS、设备名、尾点空格与junction拒绝',windows,async t=>{
  const f=await fixture(t); const write=createWindowsTrustedFileCapabilities({mode})[1]!;
  for(const name of ['../escape','C:/escape','//server/share','\\\\?\\C:\\escape','x:stream','CON','aux.txt','LPT1.txt','COM¹.txt','x.','x ','a\\b']) await assert.rejects(write.prepare({path:name,content:'x'},f.context),name);
  const outside=await mkdtemp(path.join(os.tmpdir(),'eagle-outside-')); t.after(()=>rm(outside,{recursive:true,force:true}));
  await symlink(outside,path.join(f.root,'junction'),'junction');
  await assert.rejects(write.prepare({path:'junction/escape.txt',content:'x'},f.context));
  await assert.rejects(access(path.join(outside,'escape.txt')));
  await mkdir(path.join(f.root,'nested')); const action=await write.prepare({path:'nested/x.txt',content:'x'},f.context);
  await rm(path.join(f.root,'nested'),{recursive:true}); await symlink(outside,path.join(f.root,'nested'),'junction');
  await assert.rejects(action.execute(signal()));
});
test('Windows CLI 固定argv、最小env、退出码/输出/超时/取消和重放',windows,async t=>{
  const f=await fixture(t); const code='process.stdout.write("ok")'; const cap=command(code);
  await assert.rejects(cap.prepare({profile:'fixture',argv:['-e','wrong']},f.context));
  const argv=['-e',code];const action=await cap.prepare({profile:'fixture',argv},f.context);argv[1]='process.exit(9)';
  const result=await action.execute(signal()); assert.equal(await action.verify(result,signal()),true);
  assert.equal((result.value as {cleanup:string}).cleanup,'descendants-unverified'); await assert.rejects(action.execute(signal()),/重放/);
  for(const [script,opts,pattern] of [
    ['process.stdout.write("x".repeat(10000))',{maxOutputBytes:10},/输出/],
    ['setInterval(()=>{},1000)',{timeoutMs:80},/超时/],
  ] as const){ const a=await command(script,opts).prepare({profile:'fixture',argv:['-e',script]},f.context); await assert.rejects(a.execute(signal()),pattern); }
  const idle='setInterval(()=>{},1000)', a=await command(idle).prepare({profile:'fixture',argv:['-e',idle]},f.context), controller=new AbortController();
  const pending=a.execute(controller.signal); setTimeout(()=>controller.abort(),80); await assert.rejects(pending,/取消/);
  const bad='process.exit(7)', b=await command(bad).prepare({profile:'fixture',argv:['-e',bad]},f.context); const failed=await b.execute(signal()); assert.equal(await b.verify(failed,signal()),false);
  assert.throws(()=>createWindowsTrustedCliCapability({mode,profiles:{x:{executable:process.execPath,validateArgs:()=>true,env:{PATH:'a',Path:'b'}}}}),/重复/);
});
test('Windows 自有父子进程取消后停止，测试宿主仍存活',windows,async t=>{
  const f=await fixture(t);
  const child="setTimeout(()=>require('node:fs').writeFileSync('leaked','bad'),1200);setInterval(()=>{},1000)";
  const code=`require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(child)}],{stdio:'ignore',windowsHide:true});require('node:fs').writeFileSync('ready','yes');setInterval(()=>{},1000)`;
  const a=await command(code).prepare({profile:'fixture',argv:['-e',code]},f.context), controller=new AbortController();
  const pending=a.execute(controller.signal); const checked=assert.rejects(pending,/取消/);
  for(let n=0;n<100;n++){try{await access(path.join(f.root,'ready'));break;}catch{await new Promise(r=>setTimeout(r,20));}}
  await access(path.join(f.root,'ready')); controller.abort(); await checked;
  await new Promise(r=>setTimeout(r,1400)); await assert.rejects(access(path.join(f.root,'leaked')));
});
test('Windows 审批默认拒绝、每动作重新确认；cleanup 在模型等待取消时等待完成',windows,async t=>{
  const f=await fixture(t); const write=createWindowsTrustedFileCapabilities({mode})[1]!;
  const denied=await new AgentRuntime({registry:new CapabilityRegistry().register(write)}).start({goal:'deny',workspace:f.root},new ScriptedEngine(async s=>{await s.invoke('files_write',{path:'denied',content:'x'});return '';})).result;
  assert.equal(denied.records[0]?.status,'denied'); await assert.rejects(access(path.join(f.root,'denied')));
  let approvals=0;
  const approved=await new AgentRuntime({registry:new CapabilityRegistry().register(write),authorize:async()=>++approvals===1}).start({goal:'approval binding',workspace:f.root},new ScriptedEngine(async s=>{
    await s.invoke('files_write',{path:'confirmed',content:'first'});
    await s.invoke('files_write',{path:'confirmed',content:'second'});return '';
  })).result;
  assert.equal(approvals,2);assert.deepEqual(approved.records.map(r=>r.status),['succeeded','denied']);assert.equal(await readFile(path.join(f.root,'confirmed'),'utf8'),'first');
  let cleaned=false,started!:()=>void; const ready=new Promise<void>(r=>started=r);
  const task=new AgentRuntime({registry:new CapabilityRegistry(),cleanup:async()=>{await new Promise(r=>setTimeout(r,20));cleaned=true;}}).start({goal:'cancel',workspace:f.root},new ScriptedEngine(async()=>{started();return new Promise(()=>{});}));
  await ready;task.cancel();assert.equal((await task.result).status,'cancelled');assert.equal(cleaned,true);
});
