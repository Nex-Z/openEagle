// Actual hidden Electron UI; real Pi loop, injected offline transport, temporary data only.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {createRequire}=require('node:module');
const root=path.resolve(__dirname,'..');
const {_electron:electron}=createRequire(path.join(root,'packages/agent-core/package.json'))('playwright');
const secrets=['dummy-ui-session-key-one','dummy-ui-session-key-two'];
const dirs=()=>fs.readdirSync(os.tmpdir()).filter(n=>n.startsWith('openeagle-pi-workspace-'));
const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const before=new Set(dirs());const errors=[];let profile;
 const app=await electron.launch({executablePath:process.env.OPENEAGLE_ELECTRON||require('electron'),args:[root],cwd:root,
   env:{SystemRoot:process.env.SystemRoot,TEMP:process.env.TEMP,TMP:process.env.TMP,PATH:process.env.PATH,OPEN_EAGLE_V2_PI:'1',OPEN_EAGLE_V2_PI_TEST:'1',OPENEAGLE_FIXTURE_HEADLESS:'1'},timeout:30000});
 app.process().stderr.on('data',data=>{if(String(data).includes('cleanup timeout')||secrets.some(s=>String(data).includes(s)))errors.push('unsafe stderr');});
 try{
  const page=await app.firstWindow();page.on('pageerror',e=>errors.push(e.message));
  await page.getByText('Pi 离线验收',{exact:true}).waitFor();
  await page.waitForFunction(()=>window.electronAPI.backendPort.state()==='open');
  profile=await app.evaluate(({app})=>app.getPath('userData'));
  assert.equal(path.dirname(profile).toLowerCase(),os.tmpdir().toLowerCase());assert.ok(path.basename(profile).startsWith('openeagle-v2-app-'));
  assert.equal(await app.evaluate(async({protocol})=>protocol.isProtocolHandled('asset')),false);
  await page.evaluate(()=>{window.piEvents=[];window.electronAPI.backendPort.onEvent(e=>{if(e.kind==='message')window.piEvents.push(JSON.parse(e.data));});});
  const send=page.getByRole('button',{name:'发送消息',exact:true});
  async function configure(key){
   if(!await page.locator('.pi-session-panel').evaluate(e=>e.open))await page.locator('.pi-session-panel summary').click();
   await page.getByRole('textbox',{name:'模型 API 地址',exact:true}).fill('https://fixture.invalid/v1');
   await page.getByRole('textbox',{name:'模型 ID',exact:true}).fill('offline-test');
   await page.locator('.pi-session-panel input[type=password]').fill(key);
   await page.locator('.pi-approval input').check();
   await page.getByRole('button',{name:'仅为本会话启用',exact:true}).click();
  }
  await configure(secrets[0]);await page.waitForFunction(()=>!document.querySelector('.pi-session-panel').open);
  assert.equal(await page.locator('.pi-session-panel input[type=password]').inputValue(),'');
  await page.locator('textarea').fill('读取本次临时 sample.txt，报告观察结果。');await send.click();
  await page.waitForFunction(()=>window.piEvents.some(e=>e.type==='server:message'&&e.payload.status==='completed'));
  const events=await page.evaluate(()=>window.piEvents);const terminal=events.find(e=>e.type==='server:message');
  assert.ok(events.some(e=>e.type==='server:message_delta'));assert.ok(events.some(e=>e.type==='server:trace'&&e.payload.trace.name==='workspace_read'&&e.payload.trace.status==='completed'));
  assert.equal(terminal.payload.modelStats.calls,2);assert.equal(terminal.payload.modelStats.cost,'unknown');
  assert.ok(events.every(e=>e.conversationId===terminal.conversationId&&e.requestId===terminal.requestId));
  assert.ok(!JSON.stringify(events).includes(secrets[0]));
  await page.getByText('离线 Pi 验收：已通过统一 runtime 读取临时 sample.txt。',{exact:true}).waitFor();
  await page.getByRole('button',{name:'新建会话',exact:true}).click();
  assert.equal(await page.locator('textarea').isDisabled(),true);
  assert.equal(await send.isDisabled(),true);
  await configure(secrets[1]);await page.waitForFunction(()=>!document.querySelector('.pi-session-panel').open);
  await page.locator('textarea').fill('Configured second conversation');assert.equal(await send.isEnabled(),true);
  await page.locator('.pi-session-panel summary').click();
  await page.getByRole('button',{name:'清除会话凭据',exact:true}).click();
  await page.getByText('本会话凭据已清除。',{exact:true}).waitFor();assert.equal(await send.isDisabled(),true);
  const storage=await page.evaluate(()=>JSON.stringify({...localStorage}));assert.ok(secrets.every(s=>!storage.includes(s)));
  console.log('PASS Pi offline UI: configuration, key clearing, stream, verified tool, budget, conversation isolation, explicit clear');
  // Delay only the private configure response to reproduce an old-panel callback after switching tabs.
  await app.evaluate(({ipcMain})=>{ipcMain.removeHandler('v2:model:configure');ipcMain.handle('v2:model:configure',()=>new Promise(resolve=>setTimeout(()=>resolve({configured:true}),600)));});
  await configure(secrets[0]);await page.getByRole('button',{name:'新建会话',exact:true}).click();
  await delay(850);assert.equal(await page.locator('textarea').isDisabled(),true);
  assert.equal(await send.isDisabled(),true);assert.equal(await page.locator('.pi-session-panel input[type=password]').inputValue(),'');
  console.log('PASS old conversation configure response cannot enable new conversation');
 }finally{await app.close();}
 assert.deepEqual(dirs().filter(n=>!before.has(n)),[]);assert.deepEqual(errors,[]);
 function inspect(dir){for(const item of fs.readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,item.name);if(item.isDirectory())inspect(file);else{const bytes=fs.readFileSync(file);assert.ok(secrets.every(s=>!bytes.includes(Buffer.from(s))&&!bytes.includes(Buffer.from(s,'utf16le'))),'dummy credential found in fresh profile');}}}
 inspect(profile);console.log('PASS temporary workspace cleanup and profile/log/storage contain no dummy credentials');
})().catch(error=>{console.error(error);process.exitCode=1;});
