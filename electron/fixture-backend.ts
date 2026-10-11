import { ipcMain, MessageChannelMain, utilityProcess, type BrowserWindow } from 'electron';
import { randomUUID,createHash } from 'node:crypto';
import path from 'node:path';
import type {GuiBroker} from './gui-broker';
import {createGuiFixture} from './gui-fixture';

// Matches the selectively reused preload framing. No direct desktop capability
// channel is granted to this fixture utility process.
export function attachFixtureBackend(win: BrowserWindow, options: {mode?:'fixture'|'pi'|'gui';offline?:boolean} = {}): () => Promise<void> {
  let stopCurrent: (()=>Promise<void>) | undefined;
  const shutdowns = new Set<Promise<void>>();
  let control: ((kind:string,payload:Record<string,unknown>)=>Promise<unknown>) | undefined;
  if(options.mode==='pi') for(const operation of ['configure','status','clear']) {
    ipcMain.handle(`v2:model:${operation}`,async(event,payload:unknown)=>{
      if(event.sender!==win.webContents||event.senderFrame!==win.webContents.mainFrame||!control) throw new Error('MODEL_CHANNEL_UNAVAILABLE');
      if(!payload||typeof payload!=='object'||Array.isArray(payload)||JSON.stringify(payload).length>10000)throw new Error('MODEL_CONFIGURATION_REJECTED');
      return control(`model:${operation}`,payload as Record<string,unknown>);
    });
  }
  win.webContents.on('did-finish-load',()=>{
    stopCurrent?.();
    const child=utilityProcess.fork(path.resolve(__dirname,'../packages/agent-core/dist/src/fixture-host.js'),[],{
      serviceName:'OpenEagle V2 fixture',stdio:'pipe',env:{OPEN_EAGLE_V2_MODE:options.mode??'fixture',OPEN_EAGLE_V2_GUI_MANUAL_WAIT:process.env.OPEN_EAGLE_GUI_SMOKE==='1'&&process.env.OPEN_EAGLE_GUI_MANUAL==='1'?'1':'0',OPEN_EAGLE_V2_PI_TEST:options.offline?'1':'0',SystemRoot:process.env.SystemRoot ?? '',TEMP:process.env.TEMP ?? '',TMP:process.env.TMP ?? ''},
    });
    const {port1,port2}=new MessageChannelMain();
    let gui:GuiBroker|undefined;let guiRequestId='';
    const connectionId=randomUUID();let inbound=0,outbound=0,stopped=false,exited=false;
    let stoppedPromise: Promise<void> | undefined;
    const pending=new Map<string,{resolve:(value:unknown)=>void;reject:(error:Error)=>void;timer:ReturnType<typeof setTimeout>}>();
    control=(kind,payload)=>new Promise((resolve,reject)=>{
      if(stopped||pending.size>=16){reject(new Error('MODEL_CHANNEL_UNAVAILABLE'));return;}
      const id=randomUUID();const timer=setTimeout(()=>{pending.delete(id);reject(new Error('MODEL_CHANNEL_TIMEOUT'));},5000);
      pending.set(id,{resolve,reject,timer});
      try{child.postMessage({...payload,kind,id});}catch{pending.delete(id);clearTimeout(timer);reject(new Error('MODEL_CHANNEL_UNAVAILABLE'));}
    });
    const frame=(kind:string,fields:Record<string,unknown>={})=>({channel:'open-eagle.data',protocolVersion:1,connectionId,kind,...fields});
    const stop=():Promise<void>=>{
      if(stoppedPromise)return stoppedPromise;stopped=true;gui?.cancel();
      for(const item of pending.values()){clearTimeout(item.timer);item.reject(new Error("MODEL_CHANNEL_CLOSED"));}pending.clear();
      const brokerClosing=gui?.close().then(()=>{},()=>{console.error('GUI cleanup evidence failed');})??Promise.resolve();
      stoppedPromise = new Promise<void>(resolve=>{
        if(exited){resolve();return;}
        const timer=setTimeout(()=>{console.error('Fixture cleanup timeout; forced utility termination');child.kill();resolve();},3000);
        child.once('exit',()=>{clearTimeout(timer);resolve();});
      });
      stoppedPromise=Promise.all([stoppedPromise,brokerClosing]).then(()=>{});
      shutdowns.add(stoppedPromise);
      void stoppedPromise.finally(()=>shutdowns.delete(stoppedPromise!));
      try{port1.postMessage(frame('close',{reason:'fixture_disconnected'}));}catch{/* already closed */}
      try{port1.close();}catch{/* already closed */}
      try{child.postMessage({kind:'shutdown'});}catch{/* exited */}
      return stoppedPromise;
    };
    stopCurrent=stop;
    child.on('message',(message:unknown)=>{
      if(!message||typeof message!=='object')return;
      if(stopped&&(message as {kind?:string;operation?:string}).kind!=='gui:control')return;
      const data=message as {kind?:string;envelope?:unknown;id?:string;ok?:boolean;status?:unknown;operation?:string;requestId?:string;payload?:Record<string,unknown>};
      if((options.mode==='gui'||options.mode==='pi')&&data.kind==='gui:control'){
        if(stopped&&!['close','cancel'].includes(data.operation??''))return;
        void (async()=>{
          try{let result:unknown={};const p=data.payload??{};
            if(data.operation!=='open'&&p.requestId!==guiRequestId)throw new Error('GUI_TASK_MISMATCH');
            if(data.operation==='open'){if(gui)throw new Error('GUI_DESKTOP_BUSY');guiRequestId=String(p.requestId);gui=createGuiFixture(process.env.OPEN_EAGLE_GUI_SMOKE==='1');if(process.env.OPEN_EAGLE_GUI_SMOKE==='1')(globalThis as any).__guiBroker=gui;try{result=await gui.open(guiRequestId);}catch(error){await gui.close();gui=undefined;guiRequestId='';throw error;}}
            else if(data.operation==='authorize'){if(!gui)throw new Error('GUI_NOT_OPEN');result=await gui.authorize(p as any);}
            else if(data.operation==='cancel'){gui?.cancel();}
            else if(data.operation==='close'){result=await gui?.close(p.metrics)??{};gui=undefined;guiRequestId='';}
            else throw new Error('GUI_CONTROL_REJECTED');
            child.postMessage({kind:'gui:reply',id:data.id,ok:true,result});
          }catch(error){child.postMessage({kind:'gui:reply',id:data.id,ok:false,error:error instanceof Error?error.message:'GUI_FAILED'});}
        })();return;
      }
      if((options.mode==='gui'||options.mode==='pi')&&data.kind==='capability:request'){void (async()=>{const result=gui?await gui.handle(message):{kind:'capability:result',id:data.id,ok:false,error:{code:'GUI_NOT_OPEN',message:'GUI_NOT_OPEN'}};child.postMessage(result);})();return;}
      if(data.kind==='model:reply'){
        const item=pending.get(data.id??'');if(!item)return;pending.delete(data.id!);clearTimeout(item.timer);
        if(data.ok)item.resolve(data.status);else item.reject(new Error('MODEL_CONFIGURATION_REJECTED'));return;
      }
      if(data.kind==='ready'){port1.postMessage(frame('hello'));return;}
      if(data.kind==='envelope'){port1.postMessage(frame('envelope',{seq:++outbound,envelope:data.envelope}));return;}
      if(data.kind==='protocol-error')stop();
    });
    if(options.mode!=='pi')child.stderr?.on('data',chunk=>console.error('Fixture utility:',String(chunk).slice(0,4096)));
    else child.stderr?.resume(); // Never forward provider diagnostics into application logs.
    child.once('exit',()=>{exited=true;void stop();});
    port1.on('close',stop);
    port1.on('message',event=>{
      const f=event.data;
      if(stopped||!f||f.channel!=='open-eagle.data'||f.protocolVersion!==1||f.connectionId!==connectionId){stop();return;}
      if(f.kind==='hello'){port1.postMessage(frame('hello_ack'));return;}
      if(f.kind==='hello_ack'||f.kind==='ack')return;
      if(f.kind!=='envelope'||!Number.isSafeInteger(f.seq)||f.seq<1){stop();return;}
      if(f.seq<=inbound){port1.postMessage(frame('ack',{ack:f.seq}));return;}
      let serialized: string | undefined;
      try { serialized=JSON.stringify(f.envelope); } catch { void stop();return; }
      if(f.seq!==inbound+1||!serialized||serialized.length>128_000){void stop();return;}
      if((options.mode==='gui'||options.mode==='pi')&&f.envelope?.type==='client:cancel_task'&&createHash('sha256').update(JSON.stringify([f.envelope.conversationId,f.envelope.payload?.targetRequestId])).digest('hex')===guiRequestId)gui?.cancel();
      inbound=f.seq;child.postMessage({kind:'envelope',envelope:f.envelope});port1.postMessage(frame('ack',{ack:f.seq}));
    });
    port1.start();
    win.webContents.postMessage('backend://port',{protocolVersion:1,connectionId},[port2]);
  });
  win.on('closed',()=>stopCurrent?.());
  win.webContents.on('render-process-gone',()=>stopCurrent?.());
  return async()=>{await stopCurrent?.();await Promise.allSettled([...shutdowns]);};
}
