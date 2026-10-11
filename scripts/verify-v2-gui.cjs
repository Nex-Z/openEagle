// Playwright controls only the app's consent/test API. Target input is exclusively nut-js OS input.
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const {createRequire}=require('node:module');
const root=path.resolve(__dirname,'..');const os=require('node:os');
const {_electron:electron}=createRequire(path.join(root,'packages/agent-core/package.json'))('playwright');
const expected='OpenEagle native input';
async function scenario(name){
 const pi=name==='pi';
 const app=await electron.launch({executablePath:process.env.OPENEAGLE_ELECTRON||require('electron'),args:[root],cwd:root,
  env:{SystemRoot:process.env.SystemRoot,TEMP:process.env.TEMP,TMP:process.env.TMP,PATH:process.env.PATH,...(pi?{OPEN_EAGLE_V2_PI:'1',OPEN_EAGLE_V2_PI_TEST:'1'}:{OPEN_EAGLE_V2_GUI:'1'}),OPEN_EAGLE_GUI_SMOKE:'1',OPEN_EAGLE_GUI_MANUAL:process.env.GUI_MANUAL_APPROVAL||'',OPENEAGLE_FIXTURE_HEADLESS:'1'},timeout:60000});

 let profile;const errors=[];const started=performance.now();
 try{
  profile=await app.evaluate(({app})=>app.getPath('userData'));
  const page=await app.firstWindow();page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message));
  await page.getByText(pi?'Pi 离线验收':'真实 GUI · 前台占用',{exact:true}).waitFor();await page.waitForFunction(()=>window.electronAPI.backendPort.state()==='open');
  await page.evaluate(()=>{window.guiEvents=[];window.electronAPI.backendPort.onEvent(e=>{if(e.kind==='message')window.guiEvents.push(JSON.parse(e.data));});});
  if(pi){await page.getByRole('textbox',{name:'模型 API 地址',exact:true}).fill('https://fixture.invalid/v1');await page.getByRole('textbox',{name:'模型 ID',exact:true}).fill('offline-test');await page.locator('.pi-session-panel input[type=password]').fill('dummy-gui-key');await page.locator('.pi-budget-fields input[type=number]').first().fill('8');await page.locator('.pi-approval input').check();await page.getByRole('button',{name:'仅为本会话配置',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.pi-session-panel').open);}
  await page.getByRole('button',{name:'后台优先检查',exact:true}).click();
  await page.waitForFunction(()=>window.guiEvents.some(e=>e.type==='server:message'&&e.payload.modelStats?.routing==='needsForeground'));
  assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().length),1);
  await app.evaluate(({BrowserWindow},which)=>{
    globalThis.__guiHooks={};globalThis.__guiExtra=[];
    if(which==='denied')globalThis.__guiHooks.authorize=()=>{throw new Error('GUI_APPROVAL_DENIED');};
    if(which==='stale')globalThis.__guiHooks.authorize=async()=>{await new Promise(r=>setTimeout(r,2800));};
    if(which==='closed')globalThis.__guiHooks.authorize=b=>b.testWindow().destroy();
    if(which==='focus')globalThis.__guiHooks.input=(b,phase)=>{if(phase==='after-move'){const w=new BrowserWindow({width:260,height:150,show:false,webPreferences:{nodeIntegration:false}});globalThis.__guiExtra.push(w);w.loadURL('data:text/html,Owned focus-loss fixture');w.show();}};
    if(which==='cancel'){let count=0;globalThis.__guiHooks.input=(b,phase,stage)=>{if(stage===1&&phase==='after'&&++count===5)b.cancel();};}
    if(which==='lease')globalThis.__guiHooks.authorize=async b=>{if(globalThis.__leaseChecked)return;globalThis.__leaseChecked=true;const Other=b.constructor;const other=new Other({},true);try{await other.open('second-owner');throw new Error('MUTEX_FAILED');}catch(error){if(error.message!=='GUI_DESKTOP_BUSY')throw error;}};
    if(which==='replay')globalThis.__guiHooks.authorize=async b=>{if(globalThis.__replayChecked)return;globalThis.__replayChecked=true;const q={kind:'capability:request',id:'replay-proof',requestId:b.requestId,capability:'screen.capture',payload:{operation:'oracle'}};const first=await b.handle(q);const second=await b.handle(q);if(!first.ok||second.ok||second.error.code!=='GUI_REPLAY_OR_LIMIT')throw new Error('REPLAY_FAILED');};
  },name);
  await page.getByRole('button',{name:'请求前台专用窗口验收',exact:true}).click();
  if(process.env.GUI_MANUAL_APPROVAL==='1'){
    let target;
    for(let attempt=0;attempt<100&&!target;attempt++){
      target=await app.evaluate(()=>{const w=globalThis.__guiBroker?.testWindow();return w&&!w.isDestroyed()?{title:w.getTitle(),nativeHandleHex:w.getNativeWindowHandle().toString('hex')}:null;});
      if(!target)await new Promise(resolve=>setTimeout(resolve,50));
    }
    console.log(JSON.stringify({manualTarget:target,instruction:'仅在已解锁且可暂时让出鼠键时，点击此精确标题窗口内的批准按钮。30 秒后未批准自动结束；不重抢焦点。'}));
  }

  await page.waitForFunction(()=>window.guiEvents.filter(e=>e.type==='server:message').length>=2,{},{timeout:45000});
  const events=await page.evaluate(()=>window.guiEvents);const result=events.filter(e=>e.type==='server:message').at(-1);const stats=result.payload.modelStats;
  if(['success','lease','replay','pi'].includes(name)){assert.equal(result.payload.status,'completed',JSON.stringify(result.payload));assert.equal(stats.exact,true);assert.equal(stats.state.saved,expected);assert.equal(stats.state.trustedClicks,2);assert.equal(stats.state.trustedInputs,expected.length);}
  else{assert.notEqual(result.payload.status,'completed');assert.equal(stats?.exact,false);assert.equal(stats?.state.saved,'');if(name==='cancel')assert.equal(stats.state.value,expected.slice(0,5));else assert.equal(stats.state.value,'');}
  if(pi){assert.equal(stats.calls,8);assert.equal(stats.planning,'fake-model');assert.equal(stats.steps.length,7);}
  assert.ok(stats.evidenceDirectory);const oracle=JSON.parse(fs.readFileSync(path.join(stats.evidenceDirectory,'oracle.json'),'utf8'));assert.equal(oracle.exact,stats.exact);
  assert.deepEqual(errors,[]);console.log(JSON.stringify({scenario:name,status:result.payload.status,exact:stats.exact,evidenceDirectory:stats.evidenceDirectory,state:stats.state,steps:stats.steps??stats.runtimeMetrics.steps,planning:stats.planning??'scripted',modelCalls:stats.calls??0}));
 }catch(error){console.error(JSON.stringify({scenario:name,status:'failed',durationMs:performance.now()-started,error:String(error),osInputVerified:false}));throw error;}finally{try{await app.evaluate(()=>{for(const w of globalThis.__guiExtra??[])if(!w.isDestroyed())w.destroy();});}finally{await app.close();if(profile&&path.dirname(profile).toLowerCase()===os.tmpdir().toLowerCase()&&path.basename(profile).startsWith('openeagle-v2-app-'))fs.rmSync(profile,{recursive:true,force:true});}}

}
(async()=>{for(const name of (process.env.GUI_SCENARIOS||'success,denied,stale,closed,focus,cancel,lease,replay,pi').split(','))await scenario(name);})().catch(error=>{console.error(error);process.exitCode=1;});
