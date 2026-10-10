import { FixtureSession } from './fixture-session.js';
import { createFixtureExecution } from './fixture-execution.js';
type Parent = { on(event:'message', listener:(event:{data:unknown})=>void):void; postMessage(data:unknown):void };
const parent = (process as unknown as {parentPort?:Parent}).parentPort;
if(!parent) throw new Error('fixture host requires Electron utilityProcess');
const session = new FixtureSession({send:e=>parent.postMessage({kind:'envelope',envelope:e}),create:createFixtureExecution});
parent.on('message',event=>{
  const value=event.data as {kind?:string;envelope?:unknown};
  if(value?.kind==='shutdown') { void session.close().then(()=>{parent.postMessage({kind:'stopped'});process.exit(0);}); return; }
  if(value?.kind!=='envelope') return;
  try { session.receive(value.envelope); }
  catch { parent.postMessage({kind:'protocol-error'}); }
});
parent.postMessage({kind:'ready'});
