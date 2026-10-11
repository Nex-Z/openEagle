import { performance } from 'node:perf_hooks';
import { MetricsCollector } from './metrics.js';
import { createServer } from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { mkdtemp, readFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { AgentRuntime, CapabilityRegistry } from './runtime.js';
import { ScriptedEngine } from './engines/scripted.js';
import { createBrowserCapabilities } from './adapters/browser.js';
import { createFileCapabilities } from './adapters/files.js';
import { createCliCapability } from './adapters/cli.js';
import { createWindowsTrustedFileCapabilities, createWindowsTrustedCliCapability } from './adapters/windows-trusted.js';
import type { ActionRecord, Json, TaskContext } from './contracts.js';

/** 本地可重置验收：规划是脚本，浏览器、文件、进程和独立验收均为真实执行。 */
export async function runDemo(options: {chromiumExecutable?: string; windowsMode?: 'trusted-workspace'; metricsCollector?: MetricsCollector} = {}) {
  if (process.platform === 'win32' && options.windowsMode !== 'trusted-workspace') throw new Error('Windows demo 需要显式 trusted-workspace');
  const windowsTrusted = options.windowsMode === 'trusted-workspace' && process.platform === 'win32';
  const metrics=options.metricsCollector ?? new MetricsCollector(); const started=performance.now();
  const nonce = randomUUID();
  const receipt = `receipt-${nonce}`;
  let submitted = false;
  const server = createServer((request, response) => {
    if (request.method === 'GET' && request.url === '/') {
      response.setHeader('content-type','text/html; charset=utf-8');
      response.end(`<!doctype html><meta charset="utf-8"><p id="nonce">${nonce}</p><form method="POST" action="/submit"><input name="nonce" aria-label="验收编号"><button type="submit">提交本地验收</button></form>`);
    } else if (request.method === 'POST' && request.url === '/submit') {
      let body = '';
      request.on('data', chunk => { body += String(chunk); if (body.length > 1024) request.destroy(); });
      request.on('end', () => {
        if (new URLSearchParams(body).get('nonce') !== nonce) { response.writeHead(400); response.end('invalid'); return; }
        submitted = true;
        response.setHeader('content-type','text/html; charset=utf-8'); response.end(`<p>${receipt}</p>`);
      });
    } else { response.writeHead(404); response.end(); }
  });
  await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('本地验收服务器未启动');
  const origin = `http://127.0.0.1:${address.port}`;
  const workspace = await realpath(await mkdtemp(join(tmpdir(),'openeagle-v2-demo-')));
  const browser = createBrowserCapabilities({allowedOrigins:[origin],executablePath:options.chromiumExecutable});
  const hashScript = "const fs=require('node:fs');const c=require('node:crypto');process.stdout.write(c.createHash('sha256').update(fs.readFileSync('receipt.txt')).digest('hex'))";
  const registry = new CapabilityRegistry();
  [...(windowsTrusted ? createWindowsTrustedFileCapabilities({mode:'trusted-workspace'}) : createFileCapabilities()),...browser.capabilities,(windowsTrusted ? createWindowsTrustedCliCapability : createCliCapability)({mode:'trusted-workspace',profiles:{hash_receipt:{executable:process.execPath,validateArgs:argv=>argv.length===2 && argv[0]==='-e' && argv[1]===hashScript}}})].forEach(cap=>registry.register(cap));
  let observedContext: Readonly<TaskContext> | undefined;
  const checked = (record: ActionRecord): Record<string,Json> => {
    if (record.status !== 'succeeded' || !record.output) throw new Error(`${record.capability}: ${record.status} ${record.error ?? ''}`);
    return record.output.value as Record<string,Json>;
  };
  try {
    const runtime = new AgentRuntime({registry,onMetric:metrics.collect,cleanup:()=>browser.close(),authorize:async request => {
      // 仅批准本次临时夹具的固定动作；生产宿主必须实现自己的可信批准界面。
      if (request.capability === 'browser_navigate') return request.target === `${origin}/`;
      if (request.capability === 'text_fill') return request.args !== null && typeof request.args === 'object' && !Array.isArray(request.args) && request.args.text === nonce;
      if (request.capability === 'browser_click') return request.args !== null && typeof request.args === 'object' && !Array.isArray(request.args) && request.args.expectedText === receipt;
      if (request.capability === 'files_write') return request.target === join(workspace,'receipt.txt') && !request.effects.includes('destructive');
      return request.capability === 'cli_run' && request.target.startsWith('hash_receipt:');
    }});
    const result = await runtime.start({goal:'读取本地验收编号，提交表单，保存回执并用 CLI 校验文件摘要',workspace},new ScriptedEngine(async session => {
      observedContext=session.context;
      checked(await session.invoke('browser_navigate',{url:`${origin}/`}));
      const observation=checked(await session.invoke('browser_read',{}));
      const discoveredNonce=String(observation.text).match(/[a-f0-9-]{36}/)?.[0];
      if (!discoveredNonce || discoveredNonce !== nonce) throw new Error('页面未提供编号');
      const elements=observation.elements as Record<string,Json>[];
      const input=elements.find(e=>e.tag==='input'); const button=elements.find(e=>e.tag==='button');
      if (!input || !button) throw new Error('缺少已观察表单元素');
      checked(await session.invoke('text_fill',{target:{kind:'browser-element',ref:input.ref!},text:discoveredNonce}));
      checked(await session.invoke('browser_click',{ref:button.ref!,expectedText:`receipt-${discoveredNonce}`}));
      const savedObservation=checked(await session.invoke('browser_read',{}));
      const saved=String(savedObservation.text).trim();
      checked(await session.invoke('files_write',{path:'receipt.txt',content:saved}));
      const processResult=checked(await session.invoke('cli_run',{profile:'hash_receipt',argv:['-e',hashScript]}));
      const read=checked(await session.invoke('files_read',{path:'receipt.txt'}));
      if (read.content !== saved || typeof processResult.stdout !== 'string') throw new Error('跨工具内容不一致');
      return `已完成本地脚本验收，摘要 ${processResult.stdout}`;
    })).result;
    if (result.status !== 'completed') throw new Error(`演示未完成: ${JSON.stringify(result)}`);
    // 独立验收不相信模型答案或脚本自报成功。
    const actual = await readFile(join(workspace,'receipt.txt'),'utf8');
    const expectedHash = createHash('sha256').update(actual).digest('hex');
    const cli = result.records.find(record=>record.capability==='cli_run');
    const output = cli?.output?.value as Record<string,Json> | undefined;
    if (!submitted || actual !== receipt || output?.stdout !== expectedHash || result.status !== 'completed' || result.records.some(record=>record.status !== 'succeeded')) throw new Error(`独立验收失败: ${JSON.stringify(result)}`);
    return {metrics:metrics.summary({taskId:result.taskId,runtimeStatus:result.status,taskOracle:'pass',checkpoints:['browser-nonce','server-receipt','file-bytes','cli-hash'].map(id=>({id,oracle:'pass' as const,firstAttempt:'pass' as const})),wallMs:performance.now()-started,recoveryObservationComplete:true,modelMode:'scripted'}),trace:metrics.events,planner:'scripted (非真实模型验收)',adapters:'真实 Chromium / 文件 / CLI',platform:process.platform,submitted,receipt,sha256:expectedHash,sharedEvidenceCount:observedContext?.facts.length ?? 0,result};
  } finally {
    await browser.close();
    await new Promise<void>(resolve=>server.close(()=>resolve()));
    await rm(workspace,{recursive:true,force:true});
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runDemo({chromiumExecutable:process.env.OPENEAGLE_CHROMIUM, ...(process.env.OPENEAGLE_WINDOWS_TRUSTED === '1' ? {windowsMode:'trusted-workspace' as const} : {})}).then(result=>console.log(JSON.stringify(result,null,2))).catch(error=>{console.error(error);process.exitCode=1;});
}
