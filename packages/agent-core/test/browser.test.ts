import { AgentRuntime, CapabilityRegistry } from '../src/runtime.js';
import { ScriptedEngine } from '../src/engines/scripted.js';
import { tmpdir } from 'node:os';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { existsSync } from 'node:fs';
import { test } from 'node:test';
import { createBrowserCapabilities } from '../src/adapters/browser.js';
import type { ActionOutput, Json, PreparedAction, TaskContext } from '../src/contracts.js';

const executablePath = process.env.OPENEAGLE_CHROMIUM ?? (existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : undefined);
const task: TaskContext = { id: 'browser-test', goal: '填写本地测试表单', workspace: '/tmp', facts: [] };
async function listen(server: Server): Promise<string> {
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => { server.off('error', reject); resolve(); });
  });
  const address = server.address();
  assert(address && typeof address === 'object');
  return `http://127.0.0.1:${address.port}`;
}
async function stop(server: Server): Promise<void> {
  server.closeAllConnections();
  await new Promise<void>(resolve => server.close(() => resolve()));
}
function value(output: ActionOutput): Record<string, Json> { return output.value as Record<string, Json>; }
function findRef(output: ActionOutput, tag: string): string {
  const item = (value(output).elements as Record<string, Json>[]).find(element => element.tag === tag);
  assert(item && typeof item.ref === 'string');
  return item.ref;
}
async function execute(action: PreparedAction): Promise<{ output: ActionOutput; verified: boolean }> {
  const signal = new AbortController().signal;
  const output = await action.execute(signal);
  return { output, verified: await action.verify(output, signal) };
}

test('隔离真实浏览器读取、填写、提交并以结果 DOM 验证', async () => {
  const requests: string[] = [];
  const server = createServer((request, response) => {
    requests.push(request.url ?? '');
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    if (request.url?.startsWith('/saved')) {
      const name = new URL(request.url, 'http://fixture').searchParams.get('name');
      response.end(`<body><h1>Saved ${name}</h1></body>`);
    } else response.end('<body><h1>Local fixture</h1><form action="/saved"><input name="name" aria-label="Name"><button>Save</button></form></body>');
  });
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin], timeoutMs: 5000 });
  const prepare = async (name: string, args: Json = {}) => session.capabilities.find(c => c.name === `browser_${name}`)!.prepare(args, task);
  try {
    const navigation = await prepare('navigate', { url: origin });
    assert.deepEqual(navigation.effects, ['external-submit']);
    assert.equal(requests.length, 0, 'prepare 不允许产生网络或写入');
    assert.equal((await execute(navigation)).verified, true);
    const read = await prepare('read');
    assert.deepEqual(read.resources, navigation.resources);
    const snapshot = await execute(read);
    assert.equal(snapshot.verified, true);
    assert.match(value(snapshot.output).text as string, /Local fixture/);
    const fill = await prepare('fill', { ref: findRef(snapshot.output, 'input'), value: 'Ada' });
    assert.deepEqual(fill.effects, ['write', 'external-submit']);
    assert.equal((await execute(fill)).verified, true);
    const click = await prepare('click', { ref: findRef(snapshot.output, 'button'), expectedText: 'Saved Ada', expectedUrl: `${origin}/saved?name=Ada` });
    assert.deepEqual(click.effects, ['external-submit']);
    const submitted = await execute(click);
    assert.equal(submitted.verified, true);
    assert(requests.includes('/saved?name=Ada'));
    await assert.rejects(prepare('fill', { ref: findRef(snapshot.output, 'input'), value: 'stale' }), /未观察到|变化/);
  } finally { await session.close(); await stop(server); }
});

test('无结果条件的点击不伪报成功，读取内容有界', async () => {
  const server = createServer((_request, response) => response.end(`<body><button>Idle</button><p>${'x'.repeat(1000)}</p></body>`));
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin], maxContentChars: 100 });
  const prepare = async (name: string, args: Json = {}) => session.capabilities.find(c => c.name === `browser_${name}`)!.prepare(args, task);
  try {
    await execute(await prepare('navigate', { url: origin }));
    const snapshot = await execute(await prepare('read'));
    assert.equal((value(snapshot.output).text as string).length, 100);
    const result = await execute(await prepare('click', { ref: findRef(snapshot.output, 'button') }));
    assert.equal(result.verified, false);
  } finally { await session.close(); await stop(server); }
});

test('默认拒绝未知来源，拦截跨来源子资源与重定向', async () => {
  let leaked = 0;
  const outside = createServer((_request, response) => { leaked++; response.end('outside'); });
  const outsideOrigin = await listen(outside);
  const server = createServer((request, response) => {
    if (request.url === '/redirect') { response.writeHead(302, { Location: `${outsideOrigin}/leak` }); response.end(); }
    else response.end(`<body>Safe<img src="${outsideOrigin}/image"><script>fetch('${outsideOrigin}/secret').catch(()=>{});new WebSocket('ws://127.0.0.1:${new URL(outsideOrigin).port}');</script></body>`);
  });
  const origin = await listen(server);
  const denied = createBrowserCapabilities({ executablePath });
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin], timeoutMs: 5000 });
  try {
    await assert.rejects(denied.capabilities[0]!.prepare({ url: origin }, task), /未获宿主许可/);
    await assert.rejects(session.capabilities[0]!.prepare({ url: outsideOrigin }, task), /未获宿主许可/);
    await execute(await session.capabilities[0]!.prepare({ url: origin }, task));
    await execute(await session.capabilities[1]!.prepare({}, task));
    await assert.rejects(execute(await session.capabilities[0]!.prepare({ url: `${origin}/redirect` }, task)));
    assert.equal(leaked, 0);
  } finally { await denied.close(); await session.close(); await stop(server); await stop(outside); }
});

test('取消和超时销毁上下文后才结束，不留下可继续执行的页面', async t => {
  let arrived!: () => void;
  let seen = new Promise<void>(resolve => { arrived = resolve; });
  const server = createServer((_request, _response) => arrived());
  t.after(() => stop(server));
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin], timeoutMs: 5000 });
  try {
    const action = await session.capabilities[0]!.prepare({ url: origin }, task);
    const controller = new AbortController();
    const running = action.execute(controller.signal);
    await Promise.race([seen, running]);
    controller.abort();
    await assert.rejects(running);
    await assert.rejects(session.capabilities[1]!.prepare({}, task), /已关闭/);
  } finally { await session.close(); }
  const timed = createBrowserCapabilities({ executablePath, allowedOrigins: [origin], timeoutMs: 1000 });
  seen = new Promise<void>(resolve => { arrived = resolve; });
  try {
    const action = await timed.capabilities[0]!.prepare({ url: origin }, task);
    await assert.rejects(action.execute(new AbortController().signal));
    await assert.rejects(timed.capabilities[1]!.prepare({}, task), /已关闭/);
  } finally { await timed.close(); }
});

test('准备后目标属性变化会改变指纹，旧操作拒绝执行', async () => {
  const server = createServer((_request, response) => response.end('<body><button id="target">Target</button><button onclick="document.querySelector(\'#target\').setAttribute(\'data-action\',\'changed\')">Mutate</button></body>'));
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin] });
  const prepare = async (name: string, args: Json = {}) => session.capabilities.find(c => c.name === `browser_${name}`)!.prepare(args, task);
  try {
    await execute(await prepare('navigate', { url: origin }));
    const snapshot = await execute(await prepare('read'));
    const elements = value(snapshot.output).elements as Record<string, Json>[];
    const target = elements.find(element => element.name === 'Target')!.ref;
    const mutate = elements.find(element => element.name === 'Mutate')!.ref;
    const original = await prepare('click', { ref: target });
    assert(original.fingerprint);
    await execute(await prepare('click', { ref: mutate }));
    const refreshed = await prepare('click', { ref: target });
    assert.notEqual(refreshed.fingerprint, original.fingerprint);
    await assert.rejects(original.execute(new AbortController().signal), /批准后发生变化/);
  } finally { await session.close(); await stop(server); }
});

test('审批指纹覆盖隐藏字段实时值和提交按钮覆盖目标', async () => {
  const server = createServer((_request, response) => response.end('<body><form action="/pay"><input type="hidden" name="amount" value="10" id="amount"><button id="pay">Pay</button></form><button onclick="document.querySelector(\'#amount\').value=\'999\'">Change amount</button><button onclick="document.querySelector(\'#pay\').formAction=\'/other\'">Change target</button></body>'));
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin] });
  const prepare = async (name: string, args: Json = {}) => session.capabilities.find(c => c.name === `browser_${name}`)!.prepare(args, task);
  try {
    await execute(await prepare('navigate', { url: origin }));
    const snapshot = await execute(await prepare('read'));
    const elements = value(snapshot.output).elements as Record<string, Json>[];
    const refFor = (name: string) => elements.find(element => element.name === name)!.ref;
    const original = await prepare('click', { ref: refFor('Pay') });
    await execute(await prepare('click', { ref: refFor('Change amount') }));
    const changedAmount = await prepare('click', { ref: refFor('Pay') });
    assert.notEqual(changedAmount.fingerprint, original.fingerprint, '实时 value 变化必须影响指纹');
    assert.match(changedAmount.target, /amount/);
    assert.match(changedAmount.target, /999/);
    assert(changedAmount.target.includes(`${origin}/pay`));
    await execute(await prepare('click', { ref: refFor('Change target') }));
    const changedTarget = await prepare('click', { ref: refFor('Pay') });
    assert.notEqual(changedTarget.fingerprint, changedAmount.fingerprint, '提交按钮覆盖目标必须影响指纹');
    await assert.rejects(original.execute(new AbortController().signal), /批准后发生变化/);
  } finally { await session.close(); await stop(server); }
});

test('准备阶段网页无限执行时取消仍关闭会话并结束准备', { timeout: 15_000 }, async () => {
  const server = createServer((_request, response) => response.end('<body><button id="target">Target</button><button onclick="Object.defineProperty(document.querySelector(\'#target\'),\'outerHTML\',{get(){while(true){}}})">Block evaluation</button></body>'));
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin], timeoutMs: 5000 });
  const prepare = async (name: string, args: Json = {}) => session.capabilities.find(c => c.name === `browser_${name}`)!.prepare(args, task);
  try {
    await execute(await prepare('navigate', { url: origin }));
    const snapshot = await execute(await prepare('read'));
    const elements = value(snapshot.output).elements as Record<string, Json>[];
    const target = elements.find(element => element.name === 'Target')!.ref;
    const blocker = elements.find(element => element.name === 'Block evaluation')!.ref;
    await execute(await prepare('click', { ref: blocker }));
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 100);
    try {
      await assert.rejects(session.capabilities.find(c => c.name === 'browser_click')!.prepare({ ref: target }, task, controller.signal));
    } finally { clearTimeout(timer); }
    await assert.rejects(prepare('read'), /已关闭/);
  } finally { await session.close(); await stop(server); }
});

test('可编辑文本按 DOM 文本验证填入结果', async () => {
  const server = createServer((_request, response) => response.end('<body><div contenteditable="true">Edit me</div></body>'));
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin] });
  const prepare = async (name: string, args: Json = {}) => session.capabilities.find(c => c.name === `browser_${name}`)!.prepare(args, task);
  try {
    await execute(await prepare('navigate', { url: origin }));
    const snapshot = await execute(await prepare('read'));
    const filled = await execute(await prepare('fill', { ref: findRef(snapshot.output, 'div'), value: 'Updated text' }));
    assert.equal(filled.verified, true);
  } finally { await session.close(); await stop(server); }
});

test('HTTP 重定向不转发已批准的 POST 内容，也不访问同源下一跳', async () => {
  const received: string[] = [];
  const outside = createServer((request, response) => { received.push(`outside:${request.url}`); response.end('leaked'); });
  const outsideOrigin = await listen(outside);
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    if (request.url === '/') {
      response.end('<form method="POST" action="/submit"><input name="secret" value="fixture-only"><button>Submit</button></form>');
    } else if (request.url === '/submit') {
      received.push('approved-post');
      response.writeHead(307, { Location: `${outsideOrigin}/secret` }); response.end();
    } else if (request.url === '/same-origin') {
      response.writeHead(302, { Location: '/next-hop' }); response.end();
    } else { received.push(`unexpected:${request.url}`); response.end('unexpected'); }
  });
  const origin = await listen(server);
  const session = createBrowserCapabilities({ executablePath, allowedOrigins: [origin] });
  const sameOrigin = createBrowserCapabilities({ executablePath, allowedOrigins: [origin] });
  const prepare = async (name: string, args: Json = {}) => session.capabilities.find(c => c.name === `browser_${name}`)!.prepare(args, task);
  try {
    await execute(await prepare('navigate', { url: origin }));
    const snapshot = await execute(await prepare('read'));
    // 导航错误或未验证均不可被当成成功；服务端独立核对没有第二次请求。
    const click = await prepare('click', { ref: findRef(snapshot.output, 'button'), expectedText: 'impossible-success' });
    const clicked = await execute(click).catch(() => undefined);
    if (clicked) assert.equal(clicked.verified, false);
    await assert.rejects(execute(await sameOrigin.capabilities[0]!.prepare({ url: `${origin}/same-origin` }, task)));
    assert.deepEqual(received, ['approved-post']);
  } finally { await session.close(); await sameOrigin.close(); await stop(server); await stop(outside); }
});

test('构造只接受字面 loopback fixture，能力声明限制且会话不跨任务复用', async () => {
  for (const origin of ['https://example.com', 'http://localhost:8080', 'http://192.168.1.1']) {
    assert.throws(() => createBrowserCapabilities({ allowedOrigins: [origin] }), /fixture/);
  }
  const session = createBrowserCapabilities({ allowedOrigins: ['http://127.0.0.1:12345', 'http://[::1]:12345'] });
  try {
    assert(session.capabilities.every(c => c.description.includes('受控本地 fixture')));
    await session.capabilities[0]!.prepare({ url: 'http://127.0.0.1:12345' }, task);
    await assert.rejects(session.capabilities[1]!.prepare({}, { ...task, id: 'another-task' }), /不能跨任务复用/);
  } finally { await session.close(); }
});

test('宿主cleanup覆盖审批等待期间取消并关闭后台页面', async () => {
  let ticks=0;
  const server=createServer((request,response)=>{if(request.url==='/tick'){ticks++;response.end('ok');}else{response.setHeader('content-type','text/html');response.end('<body><input><script>setInterval(()=>fetch("/tick").catch(()=>{}),20)</script></body>');}});
  const origin=await listen(server),session=createBrowserCapabilities({executablePath,allowedOrigins:[origin]});
  let waiting!:()=>void;const ready=new Promise<void>(r=>waiting=r);
  const runtime=new AgentRuntime({registry:session.capabilities.reduce((r,c)=>r.register(c),new CapabilityRegistry()),cleanup:()=>session.close(),authorize:async request=>{if(request.capability==='browser_navigate')return true;waiting();return new Promise(()=>{});}});
  try{
    const task=runtime.start({goal:'fixture cancellation',workspace:tmpdir()},new ScriptedEngine(async s=>{
      await s.invoke('browser_navigate',{url:origin});const read=await s.invoke('browser_read',{});
      const input=((read.output!.value as Record<string,Json>).elements as Record<string,Json>[]).find(e=>e.tag==='input')!;
      await s.invoke('browser_fill',{ref:input.ref!,value:'fixture'});return '';
    }));
    await ready;task.cancel();assert.equal((await task.result).status,'cancelled');
    await new Promise(r=>setTimeout(r,50));const after=ticks;await new Promise(r=>setTimeout(r,100));assert.equal(ticks,after);
    await assert.rejects(session.capabilities[1]!.prepare({}, {id:'x',goal:'x',workspace:tmpdir(),facts:[]}));
    assert.throws(()=>runtime.start({goal:'reuse',workspace:tmpdir()},new ScriptedEngine(async()=>'')),/仅允许一个任务/);
  }finally{await session.close();await stop(server);}
});
