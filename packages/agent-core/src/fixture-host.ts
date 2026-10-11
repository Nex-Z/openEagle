import { FixtureSession } from './fixture-session.js';
import { createFixtureExecution } from './fixture-execution.js';
import { ModelSessions } from './model-sessions.js';
import { createGuiExecution } from './gui-execution.js';
import { randomUUID } from 'node:crypto';
import type { CapabilityTransport } from './reused-desktop-client.js';
import { offlineModel } from './offline-model.js';
type Parent = { on(event:'message', listener:(event:{data:unknown})=>void):void; postMessage(data:unknown):void };
const parent = (process as unknown as {parentPort?:Parent}).parentPort;
if(!parent) throw new Error('host requires Electron utilityProcess');
const gui=process.env.OPEN_EAGLE_V2_MODE==='gui';
const listeners=new Set<(v:unknown)=>void>();
const pending=new Map<string,{resolve:(v:Record<string,unknown>)=>void;reject:(e:Error)=>void;timer:ReturnType<typeof setTimeout>}>();
const transport:CapabilityTransport={send:v=>parent.postMessage(v),subscribe:f=>{listeners.add(f);return()=>{listeners.delete(f);};}};
const control=(operation:string,payload:Record<string,unknown>)=>new Promise<Record<string,unknown>>((resolve,reject)=>{const id=randomUUID();const timer=setTimeout(()=>{pending.delete(id);reject(new Error('GUI_CONTROL_TIMEOUT'));},35000);pending.set(id,{resolve,reject,timer});parent.postMessage({kind:'gui:control',id,operation,payload});});
const pi=process.env.OPEN_EAGLE_V2_MODE==='pi';
const models=new ModelSessions(process.env.OPEN_EAGLE_V2_PI_TEST==='1'?offlineModel:undefined);
const session = new FixtureSession({mode:gui?'gui':pi?'pi':'fixture',send:e=>parent.postMessage({kind:'envelope',envelope:e}),create:gui?(e)=>createGuiExecution(e,transport,control):pi?(e,onText)=>e.payload.content==='GUI_REHEARSAL_BACKGROUND'?createGuiExecution(e,transport,control):models.create(e,onText,e.payload.content==='GUI_REHEARSAL_FOREGROUND'?()=>createGuiExecution(e,transport,control):undefined):(e)=>createFixtureExecution(e)});
parent.on('message',event=>{
  const raw=event.data as {kind?:string;id?:string;ok?:boolean;result?:Record<string,unknown>;error?:string};
  if(raw?.kind==='capability:result'){for(const listener of listeners)listener(raw);return;}
  if(raw?.kind==='gui:reply'){const item=pending.get(raw.id??'');if(item){pending.delete(raw.id!);clearTimeout(item.timer);if(raw.ok)item.resolve(raw.result??{});else item.reject(new Error(raw.error??'GUI_FAILED'));}return;}
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
