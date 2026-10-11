const {FixtureChannel,FocusAudit,assertOwnedState,waitForOwnedUiaStart,stopOwnedFixture}=require('./uia-start-gate.cjs');
const path=require('node:path'),fs=require('node:fs/promises'),os=require('node:os');
const {spawn}=require('node:child_process');const {pathToFileURL}=require('node:url');
const foreground=process.argv.includes('--foreground-text-only');const expectedRole=foreground?'target':'sentinel';
const scenarios=['user starts on owned sentinel; authorized target remains in background','independent target value oracle for whole Unicode/space/multiline/emoji','password/readonly/disabled and approval denial','shared desktop lease, stale observation and replay rejection','cancellation with unknown provider effects reported separately','continuous foreground-role samples; gaps cannot prove absence of transient changes'];
if(!process.argv.includes('--run-owned-window')){console.log(JSON.stringify({mode:'plan-only',opensWindow:false,acceptanceMode:foreground?'authorized-foreground-text-only':'background',scenarios:foreground?['one explicit user-started foreground Unicode write with independent readback; no retry']:scenarios},null,2));process.exit(0);}
if(process.platform!=='win32')throw Error('Windows only');
(async()=>{
 const root=path.resolve(__dirname,'..'),exe=path.join(root,'native/windows-uia/publish/framework-dependent/OpenEagle.Uia.exe');
 const {createWindowsUiaCapabilities}=await import(pathToFileURL(path.join(root,'packages/agent-core/dist/src/adapters/windows-uia.js')));
 const {acquireDesktopLease}=await import(pathToFileURL(path.join(root,'packages/agent-core/dist/src/desktop-lease.js')));
 const {AgentRuntime,CapabilityRegistry}=await import(pathToFileURL(path.join(root,'packages/agent-core/dist/src/runtime.js')));
 const {ScriptedEngine}=await import(pathToFileURL(path.join(root,'packages/agent-core/dist/src/engines/scripted.js')));
 const output=await fs.mkdtemp(path.join(os.tmpdir(),'openeagle-uia-acceptance-'));
 const report={acceptanceMode:foreground?'authorized-foreground-text-only':'background',backgroundVerified:false,startedAt:new Date().toISOString(),planning:'scripted',modelCalls:0,osUiaExecuted:false,steps:[],retries:0,output,textResult:{status:'not-run'},cancellationResult:{status:'not-run'}};
 let currentStage='startup';report.workerAudit=[];const controller=new AbortController();const stop=error=>{if(!controller.signal.aborted){if(error.message!=='FIXTURE_FINISHING')report.stopTrigger={reason:error.message,stage:currentStage,atMs:performance.now()};controller.abort(error);}};
 const focus=new FocusAudit({onViolation:stop,expectedRole});let session,child,channel,identity,exited=false,finishing=false,readyTimer,runTimer,watchdog;
 let waitForExit=Promise.resolve();let resolveReady,rejectReady;const readyPromise=new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;});
 const oracle=()=>channel.request('inspect');
 const guard=async()=>{controller.signal.throwIfAborted();const state=await oracle();assertOwnedState(state,identity);focus.assertFresh();if(state.foregroundRole!==expectedRole){focus.reject('FIXTURE_FOREGROUND_CHANGED');throw Error('FIXTURE_FOREGROUND_CHANGED');}controller.signal.throwIfAborted();return state;};
 const step=async(name,fn)=>{currentStage=name;const started=performance.now();try{await guard();const evidence=await fn();await guard();report.steps.push({name,status:'passed',firstAttempt:true,retries:0,startedAtMs:started,durationMs:performance.now()-started,evidence});}catch(error){report.steps.push({name,status:'failed',firstAttempt:false,retries:0,startedAtMs:started,durationMs:performance.now()-started,error:error.message});throw error;}};
 try{
  child=spawn(exe,[foreground?'--owned-foreground-fixture':'--owned-fixture'],{windowsHide:false,stdio:['pipe','pipe','pipe'],env:{SystemRoot:process.env.SystemRoot??'',WINDIR:process.env.WINDIR??'',TEMP:process.env.TEMP??'',TMP:process.env.TMP??'',OPENEAGLE_UIA_FIXTURE_AUTHORIZED:'1',DOTNET_GENERATE_ASPNET_CERTIFICATE:'false'}});
  waitForExit=new Promise(resolve=>{child.once('exit',()=>{exited=true;resolve();});child.once('error',()=>{exited=true;resolve();});});child.stderr.resume();
  channel=new FixtureChannel(child,{onEvent:event=>{if(event.kind==='ready'){clearTimeout(readyTimer);resolveReady(event);}else if(event.kind==='sample'){if(!finishing)focus.accept(event);}else if(!finishing)stop(Error('FIXTURE_STOPPED:'+event.reason));},onFailure:error=>{clearTimeout(readyTimer);rejectReady(error);if(!finishing)stop(error);}});
  readyTimer=setTimeout(()=>rejectReady(Error('FIXTURE_READY_TIMEOUT')),5000);
  identity=await readyPromise;
  if(!Number.isSafeInteger(identity.processId)||identity.processId!==child.pid||typeof identity.targetHwnd!=='string'||!/^\d+$/.test(identity.targetHwnd)||typeof identity.sentinelHwnd!=='string'||!/^\d+$/.test(identity.sentinelHwnd)||identity.targetHwnd===identity.sentinelHwnd||JSON.stringify(identity.automationIds)!==JSON.stringify(['entry','readonly','password','disabled']))throw Error('FIXTURE_IDENTITY_REJECTED');
  report.target={hwnd:identity.targetHwnd,processId:identity.processId};report.sentinel={hwnd:identity.sentinelHwnd};
  console.log('UIA_READY '+JSON.stringify({output,target:report.target,sentinel:report.sentinel,title:foreground?'openEagle UIA foreground text test':'openEagle UIA foreground sentinel',instruction:foreground?'Click Start one foreground text write. Keep this target in front. Stop closes the test window.':'Click Start one background UIA round on the sentinel; leave the target behind. Stop closes both windows.',startTimeoutMs:120000,runTimeoutMs:foreground?20000:60000}));
  const initial=await waitForOwnedUiaStart({read:oracle,identity,expectedRole,signal:controller.signal});report.startConfirmedAt=new Date().toISOString();focus.assertFresh();
  watchdog=setInterval(()=>{try{focus.assertFresh();}catch(error){stop(error);}},25);runTimer=setTimeout(()=>stop(Error('FIXTURE_RUN_TIMEOUT')),foreground?20000:60000);
  currentStage='bind';report.uiaBindingAttempted=true;session=await createWindowsUiaCapabilities({command:{executable:exe},target:report.target,allowedAutomationIds:identity.automationIds,observationTtlMs:10000,audit:event=>{if(report.workerAudit.length<256)report.workerAudit.push({...event,stage:currentStage});}},controller.signal);report.osUiaExecuted=true;report.leaseAcquired=true;
  const context={id:'owned-uia',goal:foreground?'authorized foreground text acceptance':'authorized background acceptance',workspace:output,facts:[]},signal=controller.signal;
  const observe=async(id='entry')=>{signal.throwIfAborted();const action=await session.capabilities[0].prepare({automationId:id},context,signal);return (await action.execute(signal)).value.observationId;};
  const prepare=async text=>session.capabilities[1].prepare({observationId:await observe(),text},context,signal);
  if(!foreground){
  await step('shared lease rejection',async()=>{try{const release=await acquireDesktopLease();await release();throw Error('LEASE_FAILED');}catch(error){if(error.message!=='GUI_DESKTOP_BUSY')throw error;}return {rejected:true};});
  const rejectionCodes={readonly:'UIA_READONLY',password:'UIA_PASSWORD_REJECTED',disabled:'UIA_DISABLED'};
  for(const id of Object.keys(rejectionCodes))await step(id+' rejection',async()=>{try{await observe(id);throw Error('EXPECTED_REJECTION');}catch(error){if(error.message!==rejectionCodes[id])throw error;return {code:error.message};}});
  await step('approval denied',async()=>{
   const registry=new CapabilityRegistry();for(const capability of session.capabilities)registry.register(capability);const runtime=new AgentRuntime({registry,authorize:async()=>false});
   const handle=runtime.start({workspace:output,goal:'deny'},new ScriptedEngine(async s=>{const o=await s.invoke('uia_observe',{automationId:'entry'});if(o.status!=='succeeded')throw Error('OBSERVE_FAILED');const r=await s.invoke('uia_set_text',{observationId:o.output.value.observationId,text:'must not write'});if(r.status!=='denied')throw Error('DENIAL_FAILED');return 'denied';}));
   const cancel=()=>handle.cancel();signal.addEventListener('abort',cancel,{once:true});if(signal.aborted)cancel();let result;try{result=await handle.result;}finally{signal.removeEventListener('abort',cancel);}if(result.status!=='completed')throw Error(result.error??result.status);const after=await oracle();if(after.text!==initial.text)throw Error('DENIED_SIDE_EFFECT');return {unchanged:true};
  });
  }
  await step('whole Unicode value',async()=>{const text='  中文 😀  \nsecond line  ';let attempted=false;try{const action=await prepare(text);attempted=true;const out=await action.execute(signal);if(!await action.verify(out,signal))throw Error('READBACK_FAILED');const after=await oracle();if(after.text!==text)throw Error('FIXTURE_VALUE_MISMATCH');report.textResult={status:'passed',expected:text,actual:after.text,independentOracle:true};return {valueMatched:true};}catch(error){report.textResult={status:'unverified',error:error.message,mayHaveChanged:attempted||error.mayHaveChanged===true||signal.aborted};throw error;}});
  if(!foreground){
  await step('stale observation rejection',async()=>{const id=await observe();await channel.request('change');try{await session.capabilities[1].prepare({observationId:id,text:'must not overwrite'},context,signal);throw Error('STALE_ACCEPTED');}catch(error){if(error.message!=='UIA_TARGET_CHANGED')throw error;}return {rejected:true};});
  await step('duplicate execution rejection',async()=>{const action=await prepare('one write'),out=await action.execute(signal);if(!await action.verify(out,signal))throw Error('READBACK_FAILED');try{await action.execute(signal);throw Error('REPLAY_ACCEPTED');}catch(error){if(error.message!=='UIA_REPLAY_OR_STALE')throw error;}return {rejected:true};});
  await step('in-flight cancellation',async()=>{const action=await prepare('cancel outcome unknown'),cancel=new AbortController(),combined=AbortSignal.any([signal,cancel.signal]);const timer=setTimeout(()=>cancel.abort(),1);report.cancellationResult={status:'attempted',outcome:'unknown'};let cancelled=false;try{await action.execute(combined);}catch(error){if(signal.aborted){report.cancellationResult={status:'interrupted',outcome:'unknown'};throw signal.reason;}if(error.message!=='UIA_CANCELLED'&&error.name!=='AbortError'){report.cancellationResult={status:'unverified',error:error.message,outcome:'unknown'};throw error;}cancelled=true;}finally{clearTimeout(timer);}if(!cancelled){report.cancellationResult={status:'not-observed',outcome:'no retry'};throw Error('CANCEL_NOT_OBSERVED');}report.cancellationResult={status:'cancel-observed',outcome:'unknown; termination does not roll back provider effects'};const after=await oracle();report.cancellationResult.actualValue=after.text;return {cancelObserved:true};});
  }
  report.status='completed';
 }catch(error){report.status='failed';report.error=error.message;process.exitCode=1;}
 finally{
  finishing=true;clearTimeout(readyTimer);clearTimeout(runTimer);clearInterval(watchdog);stop(Error('FIXTURE_FINISHING'));
  try{await session?.close();report.leaseReleased=true;}catch(error){report.cleanupError=error.message;process.exitCode=1;}
  try{if(child)await stopOwnedFixture(child,()=>exited,waitForExit);report.fixtureExited=exited;}catch(error){report.cleanupError=error.message;report.fixtureExited=false;process.exitCode=1;}
  report.focusResult=focus.report();report.finishedAt=new Date().toISOString();
  console.log((report.fixtureExited&&report.leaseReleased?'UIA_RELEASED ':'UIA_CLEANUP_INCOMPLETE ')+JSON.stringify({output,fixtureExited:report.fixtureExited,leaseReleased:report.leaseReleased??false,status:report.status,error:report.error??null,textResult:report.textResult.status,focusResult:report.focusResult.status,cancellationResult:report.cancellationResult.status}));
  await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
 }
})().catch(error=>{console.error(error.message);process.exitCode=1;});