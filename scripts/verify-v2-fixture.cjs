// Requires a local Vite server on 127.0.0.1:1420 and compiled Electron/core.
// Runs the default-off fixture flag with fresh app data, a scripted engine, and fake desktop.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');
const root = path.resolve(__dirname, '..');
const coreRequire = createRequire(path.join(root, 'packages/agent-core/package.json'));
const { _electron: electron } = coreRequire('playwright');
const fixtureDirs = () => fs.readdirSync(os.tmpdir()).filter(name => name.startsWith('openeagle-ui-fixture-'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function verify(disconnect) {
  const before = new Set(fixtureDirs());
  const app = await electron.launch({
    executablePath: process.env.OPENEAGLE_ELECTRON || require('electron'), args: [root], cwd: root,
    env: {SystemRoot:process.env.SystemRoot,TEMP:process.env.TEMP,TMP:process.env.TMP,PATH:process.env.PATH,OPEN_EAGLE_V2_FIXTURE:'1',OPENEAGLE_FIXTURE_HEADLESS:'1'},timeout:30000,
  });
  const errors = [];
  app.process().stderr.on('data', data => { if(String(data).includes('Fixture cleanup timeout')) errors.push(String(data)); });
  try {
    const page = await app.firstWindow();
    page.on('pageerror', error => errors.push(error.message));
    await page.getByText('V2 测试模式',{exact:true}).waitFor();
    await page.waitForFunction(()=>window.electronAPI.backendPort.state()==='open');
    const profile = await app.evaluate(({app})=>app.getPath('userData'));
    assert.equal(path.dirname(profile).toLowerCase(),os.tmpdir().toLowerCase());
    assert.ok(path.basename(profile).startsWith('openeagle-v2-app-'));
    assert.equal(await page.evaluate(()=>{try{window.electronAPI.convertFileSrc('fixture-denied');return false;}catch{return true;}}),true);
    assert.equal(await app.evaluate(async({protocol})=>protocol.isProtocolHandled('asset')),false);
    await page.evaluate(()=>{window.fixtureEvents=[];window.electronAPI.backendPort.onEvent(e=>{if(e.kind==='message')window.fixtureEvents.push(JSON.parse(e.data));});});
    async function send(content) {
      await page.locator('textarea').fill(content);
      await page.getByRole('button',{name:'发送消息',exact:true}).click();
      await page.getByText('fixture_desktop_click 需要工具确认',{exact:true}).waitFor();
    }
    if(!disconnect) {
      await send('Run one simulated fixture action');
      await page.getByRole('button',{name:'新建会话',exact:true}).click();
      await page.getByRole('button',{name:'允许',exact:true}).first().click();
      await page.waitForFunction(()=>window.fixtureEvents.some(e=>e.type==='server:message'&&e.payload.status==='completed'));
      // Result remains in original conversation even when confirmation is given from another tab.
      await delay(200);
      assert.equal(await page.getByText('TEST MODE: one simulated click verified. No model or real desktop was used.',{exact:true}).count(),0);
      await send('Cancel this fixture');
      await page.getByRole('button',{name:'取消测试任务',exact:true}).click();
      await page.getByText('Fixture cancelled; cleanup complete.',{exact:true}).waitFor();
      const events=await page.evaluate(()=>window.fixtureEvents);
      const terminal=events.filter(e=>e.type==='server:message');
      assert.deepEqual(terminal.map(e=>e.payload.status),['completed','cancelled']);
      for(const result of terminal) {
        const flow=events.filter(e=>e.requestId===result.requestId);
        assert.ok(flow.every(e=>e.conversationId===result.conversationId));
        assert.deepEqual(flow.map(e=>e.type),['server:status','server:agent_progress','server:tool_confirmation_required','server:trace','server:message']);
      }
      console.log('PASS UI correlated lifecycle, cross-conversation confirmation/result, cancellation');
    }
    await send(disconnect ? 'Disconnect pending fixture' : 'Close pending fixture');
    assert.ok(fixtureDirs().some(name=>!before.has(name)));
    if(disconnect) {
      await page.evaluate(()=>window.electronAPI.backendPort.close());
      for(let i=0;i<100&&fixtureDirs().some(name=>!before.has(name));i++) await delay(30);
      assert.deepEqual(fixtureDirs().filter(name=>!before.has(name)),[]);
    }
  } finally { await app.close(); }
  assert.deepEqual(fixtureDirs().filter(name=>!before.has(name)),[]);
  assert.deepEqual(errors,[]);
  console.log(`PASS ${disconnect?'port disconnect':'window close'} cancels pending task and removes execution directory`);
}
(async()=>{await verify(false);await verify(true);})().catch(error=>{console.error(error);process.exitCode=1;});
