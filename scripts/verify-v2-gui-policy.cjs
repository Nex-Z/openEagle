const test=require('node:test');const assert=require('node:assert/strict');
const {GuiAdmission,GuiConsent,activateGuiWindowOnce}=require('../dist-electron/gui-policy.js');
const {CapabilityTicketStore}=require('../dist-electron/capability-ticket-store.js');
test('GUI rejects duplicate IDs and stopped leases',()=>{const p=new GuiAdmission();p.admit('a');assert.throws(()=>p.admit('a'),/REPLAY/);p.stop();assert.throws(()=>p.admit('b'),/STOPPED/);});
test('GUI observation checks expiry and independent state changes',()=>{let now=0;const p=new GuiAdmission(()=>now);p.observe('o',{text:'a'});p.check('o',{text:'a'});assert.throws(()=>p.check('o',{text:'b'}),/TARGET_CHANGED/);now=2501;assert.throws(()=>p.check('o',{text:'a'}),/STALE/);});
test('GUI tickets bind request, arguments, epoch, and can be consumed only once',()=>{const p=new CapabilityTicketStore();const epoch=p.beginAuthorityEpoch();const args={target:'owned',x:10,y:20};const r={confirmationId:'one',requestId:'r',capability:'desktop.mouse',payload:args};assert.equal(p.grant('one','r','desktop.mouse',args,epoch),true);assert.equal(p.consume({...r,requestId:'other'},epoch),false);assert.equal(p.consume({...r,payload:{...args,x:11}},epoch),false);assert.equal(p.consume(r,epoch),false);assert.equal(p.grant('one','r','desktop.mouse',args,epoch),false);assert.equal(p.grant('fresh','r','desktop.mouse',args,epoch),true);assert.equal(p.consume({...r,confirmationId:'fresh'},epoch),true);assert.equal(p.consume({...r,confirmationId:'fresh'},epoch),false);});
test('GUI approval is not implicit and revocation invalidates a granted ticket',()=>{const p=new CapabilityTicketStore();const epoch=p.beginAuthorityEpoch();const r={confirmationId:'one',requestId:'r',capability:'desktop.mouse',payload:{}};p.register('one','r','desktop.mouse',{},epoch);assert.equal(p.consume(r,epoch),false);p.grant('two','r','desktop.mouse',{},epoch);p.revokeAuthorityEpoch(epoch);assert.equal(p.consume({...r,confirmationId:'two'},epoch),false);});

test('production UI approval and conversation test approval are distinct; production rejects test source',()=>{const ui=new GuiConsent(false);assert.equal(ui.approved,false);assert.throws(()=>ui.grant('conversation-fixture-test'),/TEST_APPROVAL_DISABLED/);ui.grant('owned-window-ui-ipc');assert.equal(ui.source,'owned-window-ui-ipc');assert.equal(ui.approved,true);ui.revoke();assert.throws(()=>ui.grant('owned-window-ui-ipc'),/STOPPED/);const fixture=new GuiConsent(true);fixture.grant('conversation-fixture-test');assert.equal(fixture.source,'conversation-fixture-test');fixture.revoke();assert.equal(fixture.approved,false);assert.throws(()=>fixture.grant('conversation-fixture-test'),/STOPPED/);});

test('native activation return values never substitute for independent focus verification',async()=>{for(const returned of [undefined,false,true]){let activations=0,checks=0;const result=await activateGuiWindowOnce(async()=>{activations++;return returned;},async()=>{checks++;},new AbortController().signal);assert.equal(result,typeof returned);assert.equal(activations,1);assert.equal(checks,1);}});
test('activation identity failure and cancellation never trigger another activation',async()=>{let attempts=0;await assert.rejects(activateGuiWindowOnce(async()=>{attempts++;},async()=>{throw new Error('GUI_NATIVE_HANDLE_MISMATCH');},new AbortController().signal),/HANDLE_MISMATCH/);assert.equal(attempts,1);const c=new AbortController();c.abort();await assert.rejects(activateGuiWindowOnce(async()=>{attempts++;},async()=>{},c.signal));assert.equal(attempts,1);});

const {waitForGuiTerminal}=require('./gui-terminal-wait.cjs');
test('GUI terminal waiter reports release once and fails missing terminal within three seconds',async()=>{let time=0,releases=0;await assert.rejects(waitForGuiTerminal({read:async()=>({release:{releasedAt:'now'},terminal:false}),onRelease:()=>releases++,timeoutMs:330000,now:()=>time,sleep:async ms=>{time+=ms;}}),/MISSING_AFTER_RELEASE/);assert.equal(time,3000);assert.equal(releases,1);});
test('GUI terminal waiter fails transport errors immediately and accepts terminal',async()=>{let reads=0;await assert.rejects(waitForGuiTerminal({read:async()=>{reads++;return {failure:{kind:'close'}};},onRelease:()=>{},timeoutMs:330000}),/TRANSPORT_FAILED/);assert.equal(reads,1);await waitForGuiTerminal({read:async()=>({terminal:true}),onRelease:()=>{},timeoutMs:100});});
test('manual broker report passes the unchanged strict preload validator without native activation',async()=>{
 const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
 const brokerFile=path.resolve(__dirname,'../dist-electron/gui-broker.js');
 const originalRequire=require('node:module').createRequire(brokerFile);
 const context={exports:{},AbortController,performance,require:id=>id==='electron'?{ipcMain:{removeListener(){}}}:id==='@nut-tree-fork/nut-js'||id==='./input'||id==='./screenshot'?{}:originalRequire(id)};
 vm.runInNewContext(fs.readFileSync(brokerFile,'utf8'),context);
 const broker=new context.exports.GuiBroker({inspect:async()=>({}),summary:()=>({})},true);
 broker.recordPointer({x:150,y:300},{x:150,y:300},'keyboard:before');
 assert.throws(()=>broker.recordPointer({x:150,y:300},{x:151,y:300},'keyboard:after'),/GUI_POINTER_TAKEOVER/);
 const report=await broker.close();
 assert.equal(report.pointerAudit.length,2);assert.equal(report.pointerAudit[1].delta.x,1);assert.equal(report.pointerAudit[1].phase,'keyboard:after');assert.equal(report.stopReason,'GUI_POINTER_TAKEOVER');
 const validation={exports:{},require:()=>({})};const preload=fs.readFileSync(path.resolve(__dirname,'../dist-electron/preload.js'),'utf8');vm.runInNewContext(preload.slice(0,preload.indexOf('const BACKEND_PORT_CHANNEL')),validation);
 const envelope=stats=>({type:'server:message',requestId:'r',conversationId:'c',timestamp:new Date().toISOString(),payload:{modelStats:stats}});
 assert.equal(validation.isValidRendererEnvelope(envelope({...report,nativeFocusReturnType:undefined})),false);
 assert.equal(validation.isValidRendererEnvelope(envelope(report)),true);
 assert.ok(broker.testReleaseStatus().releasedAt);assert.equal(report.nativeFocusReturnType,null);
});
