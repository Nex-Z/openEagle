// 自有窗口的只读诊断：无 focus、截图或输入。PowerShell 只查询系统元数据。
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {execFile}=require('node:child_process');const {promisify}=require('node:util');const root=path.resolve(__dirname,'..');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'openeagle-gui-diagnostic-'));
const host=path.join(dir,'host.cjs');const output=path.join(dir,'diagnostic.json');
fs.writeFileSync(host,`
const {app,BrowserWindow}=require('electron');const fs=require('node:fs'),path=require('node:path');const {createRequire}=require('node:module');const {execFileSync}=require('node:child_process');
const root=process.env.OPENEAGLE_DIAG_ROOT;const nut=createRequire(path.join(root,'package.json'))('@nut-tree-fork/nut-js');
app.setPath('userData',path.join(process.env.OPENEAGLE_DIAG_DIR,'profile'));app.disableHardwareAcceleration();
app.whenReady().then(async()=>{let win;try{
 win=new BrowserWindow({title:'OpenEagle read-only HWND diagnosis',width:480,height:240,show:false,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});
 win.on('page-title-updated',e=>e.preventDefault());await win.loadURL('data:text/html,<h2>OpenEagle owned diagnostic window</h2><p>No input, focus, or screenshot actions.</p>');win.showInactive();await new Promise(r=>setTimeout(r,200));
 const bytes=win.getNativeWindowHandle();const handle=bytes.length===8?Number(bytes.readBigUInt64LE()):bytes.readUInt32LE();
 const native=await nut.getWindows();const active=await nut.getActiveWindow();const found=native.find(w=>w.windowHandle===handle);
 const osInfo=JSON.parse(execFileSync(process.env.OPENEAGLE_DIAG_PS,['-NoProfile','-NonInteractive','-File',path.join(root,'scripts','diagnose-v2-window.ps1'),'-TargetHandle',String(handle)],{encoding:'utf8',windowsHide:true,timeout:15000}));
 const result={kind:'read-only-owned-window',inputCalls:0,activationCalls:0,screenshotCalls:0,pid:process.pid,title:win.getTitle(),bufferLength:bytes.length,handleHex:bytes.toString('hex'),handleNumber:handle,electronFocused:win.isFocused(),nativeWindowCount:native.length,nativeMatch:!!found,nativeHandleTypes:[...new Set(native.map(w=>typeof w.windowHandle))],nativeActiveHandle:active.windowHandle,nativeOwnTitle:found?await found.getTitle():null,nativeOwnRegion:found?await found.getRegion():null,os:osInfo};
 fs.writeFileSync(process.env.OPENEAGLE_DIAG_OUTPUT,JSON.stringify(result,null,2));
 }catch(error){fs.writeFileSync(process.env.OPENEAGLE_DIAG_OUTPUT,JSON.stringify({error:String(error)}));process.exitCode=1;}finally{if(win&&!win.isDestroyed())win.destroy();app.quit();}});
`);
(async()=>{try{const executable=process.env.OPENEAGLE_ELECTRON;if(!executable)throw new Error('Set OPENEAGLE_ELECTRON to the existing binary');await promisify(execFile)(executable,[host],{cwd:root,windowsHide:true,timeout:25000,env:{SystemRoot:process.env.SystemRoot,TEMP:process.env.TEMP,TMP:process.env.TMP,PATH:process.env.PATH,OPENEAGLE_DIAG_PS:process.env.OPENEAGLE_POWERSHELL||path.join(process.env.ProgramFiles,'PowerShell','7','pwsh.exe'),OPENEAGLE_DIAG_ROOT:root,OPENEAGLE_DIAG_DIR:dir,OPENEAGLE_DIAG_OUTPUT:output}});const result=JSON.parse(fs.readFileSync(output,'utf8'));console.log(JSON.stringify(result,null,2));console.log('Evidence: '+output);if(result.error)throw new Error(result.error);}finally{const profile=path.join(dir,'profile');if(path.dirname(dir)===os.tmpdir()&&path.basename(dir).startsWith('openeagle-gui-diagnostic-')){fs.rmSync(profile,{recursive:true,force:true});fs.rmSync(host,{force:true});}}})().catch(e=>{console.error(String(e));process.exitCode=1;});
