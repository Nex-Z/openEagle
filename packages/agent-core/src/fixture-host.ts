import { FixtureSession } from './fixture-session.js';
import { createFixtureExecution } from './fixture-execution.js';
import { ModelSessions } from './model-sessions.js';
import { offlineModel } from './offline-model.js';
type Parent = { on(event:'message', listener:(event:{data:unknown})=>void):void; postMessage(data:unknown):void };
const parent = (process as unknown as {parentPort?:Parent}).parentPort;
if(!parent) throw new Error('host requires Electron utilityProcess');
const pi=process.env.OPEN_EAGLE_V2_MODE==='pi';
const models=new ModelSessions(process.env.OPEN_EAGLE_V2_PI_TEST==='1'?offlineModel:undefined);
const session = new FixtureSession({mode:pi?'pi':'fixture',send:e=>parent.postMessage({kind:'envelope',envelope:e}),create:pi?(e,onText)=>models.create(e,onText):(e)=>createFixtureExecution(e)});
parent.on('message',event=>{
  const value=event.data as {kind?:string;envelope?:unknown;id?:string;conversationId?:unknown;config?:unknown;credential?:unknown};
  if(value?.kind==='shutdown') { void session.close().then(()=>{models.close();parent.postMessage({kind:'stopped'});process.exit(0);}); return; }
  if(['model:configure','model:status','model:clear'].includes(value?.kind??'')&&pi){
    try {
      const status=value.kind==='model:configure'?models.configure(value.conversationId,value.config,value.credential):value.kind==='model:clear'?models.clear(value.conversationId):models.status(value.conversationId);
      parent.postMessage({kind:'model:reply',id:value.id,ok:true,status});
    } catch {parent.postMessage({kind:'model:reply',id:value.id,ok:false,error:'MODEL_CONFIGURATION_REJECTED'});}
    return;
  }
  if(value?.kind!=='envelope') return;
  try { session.receive(value.envelope); }
  catch { parent.postMessage({kind:'protocol-error'}); }
});
parent.postMessage({kind:'ready'});
