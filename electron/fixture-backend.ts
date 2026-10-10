import { MessageChannelMain, utilityProcess, type BrowserWindow } from 'electron';
import { randomUUID } from 'node:crypto';
import path from 'node:path';

// Matches the selectively reused preload framing. No direct desktop capability
// channel is granted to this fixture utility process.
export function attachFixtureBackend(win: BrowserWindow): () => Promise<void> {
  let stopCurrent: (()=>Promise<void>) | undefined;
  const shutdowns = new Set<Promise<void>>();
  win.webContents.on('did-finish-load',()=>{
    stopCurrent?.();
    const child=utilityProcess.fork(path.resolve(__dirname,'../packages/agent-core/dist/src/fixture-host.js'),[],{
      serviceName:'OpenEagle V2 fixture',stdio:'pipe',env:{SystemRoot:process.env.SystemRoot ?? '',TEMP:process.env.TEMP ?? '',TMP:process.env.TMP ?? ''},
    });
    const {port1,port2}=new MessageChannelMain();
    const connectionId=randomUUID();let inbound=0,outbound=0,stopped=false,exited=false;
    let stoppedPromise: Promise<void> | undefined;
    const frame=(kind:string,fields:Record<string,unknown>={})=>({channel:'open-eagle.data',protocolVersion:1,connectionId,kind,...fields});
    const stop=():Promise<void>=>{
      if(stoppedPromise)return stoppedPromise;stopped=true;
      stoppedPromise = new Promise<void>(resolve=>{
        if(exited){resolve();return;}
        const timer=setTimeout(()=>{console.error('Fixture cleanup timeout; forced utility termination');child.kill();resolve();},3000);
        child.once('exit',()=>{clearTimeout(timer);resolve();});
      });
      shutdowns.add(stoppedPromise);
      void stoppedPromise.finally(()=>shutdowns.delete(stoppedPromise!));
      try{port1.postMessage(frame('close',{reason:'fixture_disconnected'}));}catch{/* already closed */}
      try{port1.close();}catch{/* already closed */}
      try{child.postMessage({kind:'shutdown'});}catch{/* exited */}
      return stoppedPromise;
    };
    stopCurrent=stop;
    child.on('message',(message:unknown)=>{
      if(stopped||!message||typeof message!=='object')return;
      const data=message as {kind?:string;envelope?:unknown};
      if(data.kind==='ready'){port1.postMessage(frame('hello'));return;}
      if(data.kind==='envelope'){port1.postMessage(frame('envelope',{seq:++outbound,envelope:data.envelope}));return;}
      if(data.kind==='protocol-error')stop();
    });
    child.stderr?.on('data',chunk=>console.error('Fixture utility:',String(chunk).slice(0,4096)));
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
      inbound=f.seq;child.postMessage({kind:'envelope',envelope:f.envelope});port1.postMessage(frame('ack',{ack:f.seq}));
    });
    port1.start();
    win.webContents.postMessage('backend://port',{protocolVersion:1,connectionId},[port2]);
  });
  win.on('closed',()=>stopCurrent?.());
  win.webContents.on('render-process-gone',()=>stopCurrent?.());
  return async()=>{await stopCurrent?.();await Promise.allSettled([...shutdowns]);};
}
