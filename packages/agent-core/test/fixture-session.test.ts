import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { FixtureSession, type SessionEnvelope } from '../src/fixture-session.js';
import { createFixtureExecution } from '../src/fixture-execution.js';
const envelope = (type='client:send_message', requestId='r1', conversationId='c1', payload:Record<string,unknown>={content:'fixture'}) => ({type,requestId,conversationId,payload,timestamp:new Date().toISOString()});
async function waitFor(events: SessionEnvelope[], type: string) {
  for(let i=0;i<100;i++){const e=events.find(e=>e.type===type);if(e)return e;await delay(5);}
  throw new Error(`missing ${type}`);
}
test('session start/progress/confirmation/trace/result correlate; retries and decisions execute once',async()=>{
  const events:SessionEnvelope[]=[];let executed=0,cleaned=0;
  const session=new FixtureSession({send:e=>events.push(e),create:e=>createFixtureExecution(e,{executed:()=>executed++,cleaned:()=>cleaned++})});
  const start=envelope();session.receive(start);session.receive(start);
  const confirm=await waitFor(events,'server:tool_confirmation_required');
  const id=(confirm.payload.confirmation as {confirmationId:string}).confirmationId;
  session.receive(envelope('client:tool_confirmation','confirm','wrong',{confirmationId:id,decision:'allow'}));
  await delay(10);assert.equal(executed,0);
  const decision=envelope('client:tool_confirmation','confirm','c1',{confirmationId:id,decision:'allow'});
  session.receive(decision);session.receive(decision);await session.idle();session.receive(start);
  assert.equal(executed,1);assert.equal(cleaned,1);
  assert.deepEqual(events.map(e=>e.type),['server:status','server:agent_progress','server:tool_confirmation_required','server:trace','server:message']);
  assert.ok(events.every(e=>e.requestId==='r1'&&e.conversationId==='c1'));
  for(const event of events)assert.deepEqual(JSON.parse(JSON.stringify(event)),event,'wire payload must contain only JSON values');
  assert.equal(events.at(-1)!.payload.status,'completed');await session.close();assert.equal(cleaned,1);
});
test('confirmation wait cancellation cleans before exactly one terminal event; stale decision cannot execute',async()=>{
  const events:SessionEnvelope[]=[];let executed=0,cleaned=0;
  const session=new FixtureSession({send:e=>{if(e.type==='server:message')assert.equal(cleaned,1);events.push(e);},create:e=>createFixtureExecution(e,{executed:()=>executed++,cleaned:()=>cleaned++})});
  session.receive(envelope());const confirmation=await waitFor(events,'server:tool_confirmation_required');
  session.receive(envelope('client:cancel_task','cancel','c1',{targetRequestId:'r1'}));await session.idle();
  session.receive(envelope('client:tool_confirmation','confirm','c1',{confirmationId:(confirmation.payload.confirmation as {confirmationId:string}).confirmationId,decision:'allow'}));
  assert.equal(executed,0);assert.equal(cleaned,1);assert.equal(events.filter(e=>e.type==='server:message').length,1);
  assert.equal(events.at(-1)!.payload.status,'cancelled');await session.close();
});
test('immediate cancellation, active duplicate clicks, bounded retention and disconnect cleanup',async()=>{
  let executed=0,cleaned=0;const events:SessionEnvelope[]=[];
  const session=new FixtureSession({maxRequests:1,send:e=>events.push(e),create:e=>createFixtureExecution(e,{executed:()=>executed++,cleaned:()=>cleaned++})});
  session.receive(envelope());session.receive(envelope('client:send_message','r2'));
  session.receive(envelope('client:cancel_task','cancel','c1',{targetRequestId:'r1'}));await session.idle();
  assert.equal(executed,0);assert.equal(cleaned,1);assert.equal(events.filter(e=>e.type==='server:message').length,1);
  assert.throws(()=>session.receive(envelope('client:send_message','r3')),/capacity/);await session.close();
  const other=new FixtureSession({send:()=>{},create:e=>createFixtureExecution(e,{cleaned:()=>cleaned++})});
  other.receive(envelope());await other.close();assert.equal(cleaned,2);
});

test('cleanup failure is reported without retry or false cleanup success',async()=>{
  for(const cancel of [false,true]){
    const events:SessionEnvelope[]=[];let cleanupCalls=0;
    const session=new FixtureSession({send:e=>events.push(e),create:async e=>{
      const execution=await createFixtureExecution(e);
      return {...execution,engine:{name:'no-tools',async run(){return 'done';}},async cleanup(){cleanupCalls++;await execution.cleanup();throw new Error('injected cleanup failure');}};
    }});
    session.receive(envelope());
    if(cancel)session.receive(envelope('client:cancel_task','cancel','c1',{targetRequestId:'r1'}));
    await session.idle();assert.equal(cleanupCalls,1);
    const results=events.filter(e=>e.type==='server:message');assert.equal(results.length,1);
    assert.equal(results[0]!.payload.status,'failed');assert.match(String(results[0]!.payload.content),/injected cleanup failure/);
    assert.doesNotMatch(String(results[0]!.payload.content),/cleanup complete/);await session.close();
  }
});

test('Pi entry reports safe actionable UIA failures without provider details or false cleanup claims',async()=>{for(const code of ['UIA_TARGET_CHANGED','UIA_TIMEOUT','UIA_CLEANUP_INCOMPLETE']){const events:SessionEnvelope[]=[];const session=new FixtureSession({mode:'pi',send:e=>events.push(e),create:async()=>{throw Error(code+' secret-provider-detail');}});session.receive(envelope());await session.idle();const terminal=events.find(e=>e.type==='server:message')!;assert.equal(terminal.payload.status,'failed');assert.match(String(terminal.payload.content),new RegExp(code));assert.doesNotMatch(String(terminal.payload.content),/secret-provider-detail|MODEL_RUNTIME_FAILED|cleanup complete/);await session.close();}});
