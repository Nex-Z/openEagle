import test from 'node:test';
import assert from 'node:assert/strict';
import { access, link, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createAssistantMessageEventStream, type AssistantMessage, type TranscriptContext } from '@earendil-works/pi-ai';
import type { StreamFn } from '@earendil-works/pi-agent-core';
import { parseModelConfig, configuredModel, estimateCost } from '../src/model-config.js';
import { createProviderStream, ModelCallBudget } from '../src/provider-stream.js';
import { ModelSessions } from '../src/model-sessions.js';
import { FixtureSession, type SessionEnvelope } from '../src/fixture-session.js';
import { AgentRuntime } from '../src/runtime.js';
import { readonlyWorkspace } from '../src/readonly-workspace.js';
import { offlineModel } from '../src/offline-model.js';

const config=parseModelConfig({provider:'openai-like',api:'openai-completions',endpoint:'https://fixture.invalid/v1',modelId:'offline-test',maxCalls:4,maxOutputTokens:256,timeoutMs:1000,maxRetries:0,workspace:'temporary-sample',approved:true});
const secret='dummy-session-only-key';
const model=configuredModel(config);
function message(text='done',stopReason:AssistantMessage['stopReason']='stop'):AssistantMessage {
  return {role:'assistant',content:[{type:'text',text}],api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),stopReason,
    usage:{input:10,output:12,cacheRead:0,cacheWrite:0,totalTokens:22,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
}
function stream(m=message(),deltas:string[]=[]){
  const s=createAssistantMessageEventStream();
  for(const delta of deltas)s.push({type:'text_delta',contentIndex:0,delta,partial:m});
  if(m.stopReason==='error'||m.stopReason==='aborted')s.push({type:'error',reason:m.stopReason,error:m});
  else s.push({type:'done',reason:m.stopReason as 'stop'|'toolUse',message:m});
  return s;
}
async function collect(fn:StreamFn,signal?:AbortSignal){
  const events=[];const source=await fn(model,{messages:[]} as unknown as TranscriptContext,{signal});
  for await(const event of source)events.push(event);
  return {events,result:await source.result()};
}
const envelope=(requestId='r1',conversationId='c1',content='Read the selected sample'):SessionEnvelope=>({type:'client:send_message',requestId,conversationId,payload:{content},timestamp:new Date().toISOString()});
async function run(store:ModelSessions,e=envelope(),onText=(_text:string)=>{}){
  const execution=await store.create(e,onText);
  const result=await new AgentRuntime({registry:execution.registry,cleanup:execution.cleanup}).start({goal:String(e.payload.content),workspace:execution.workspace},execution.engine).result;
  await assert.rejects(access(execution.workspace));return result;
}

test('explicit provider/API compatibility fails closed and validates endpoint, consent and budgets',()=>{
  for(const [provider,api] of [['openai','openai-responses'],['openai-like','openai-completions'],['anthropic','anthropic-messages']]){
    assert.equal(configuredModel(parseModelConfig({...config,provider,api})).api,api);
  }
  for(const bad of [{provider:'unknown'},{provider:'openai'},{api:'anthropic-messages'},{endpoint:'http://fixture.invalid'},
    {endpoint:'https://user:pass@fixture.invalid'},{endpoint:'https://fixture.invalid/?key=bad'},{endpoint:'https://fixture.invalid/#fragment'},
    {maxCalls:5},{maxCalls:0},{maxOutputTokens:2049},{timeoutMs:999},{maxRetries:1},{approved:false},{workspace:'C:/'},{extra:true}]){
    assert.throws(()=>parseModelConfig({...config,...bad}));
  }
});

test('host passes explicit key, finite options, no retries and guarded same-endpoint fetch',async()=>{
  let fetched=0;
  const fakeFetch:typeof fetch=async(_resource,init)=>{fetched++;assert.equal(init?.redirect,'error');return new Response('fixture');};
  const transport:StreamFn=async(_model,_context,options)=>{
    assert.equal(options?.apiKey,secret);assert.equal(options?.maxTokens,256);assert.equal(options?.maxRetries,0);
    assert.equal(options?.timeoutMs,1000);assert.equal(options?.transport,'sse');assert.deepEqual(options?.env,{});
    await assert.rejects(options!.fetch!('https://other.invalid/v1'),/ENDPOINT_BLOCKED/);
    await assert.rejects(options!.fetch!('https://fixture.invalid/v10'),/ENDPOINT_BLOCKED/);
    assert.equal(await (await options!.fetch!('https://fixture.invalid/v1/chat/completions')).text(),'fixture');return stream();
  };
  assert.equal((await collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport,fetch:fakeFetch}))).result.stopReason,'stop');
  assert.equal(fetched,1);
});

test('failed calls consume budget; errors never echo credentials and no transport retry happens',async()=>{
  let calls=0;const budget=new ModelCallBudget(1);
  const fn=createProviderStream({config,credential:secret,budget,transport:()=>{calls++;throw new Error(secret);}});
  const first=await collect(fn),second=await collect(fn);
  assert.equal(first.result.errorMessage,'MODEL_PROVIDER_FAILED');assert.equal(second.result.errorMessage,'MODEL_CALL_BUDGET_EXHAUSTED');
  assert.equal(calls,1);assert.equal(budget.calls,1);assert.ok(!JSON.stringify([first,second]).includes(secret));
});

test('split chunks and split final text blocks redact before public output',async()=>{
  const m=message();m.content=[{type:'text',text:'dummy-session-'},{type:'text',text:'only-key'}];const text:string[]=[];
  const result=await collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport:()=>stream(m,['dummy-','session-only-','key']),onText:t=>text.push(t)}));
  assert.equal(text.join(''),'[REDACTED]');assert.ok(!JSON.stringify(result).includes(secret));
  assert.deepEqual(result.result.content,[{type:'text',text:'[REDACTED]'}]);
});

test('one task redactor spans tool-use model turns, preventing cross-turn credential reconstruction',async()=>{
  let calls=0;const chunks:string[]=[];
  const store=new ModelSessions(()=>{
    const first=++calls===1;const m=message(first?'dummy-session-':'only-key',first?'toolUse':'stop');
    if(first)m.content.push({type:'toolCall',id:'read1',name:'workspace_read',arguments:{path:'sample.txt'}});
    return stream(m,[first?'dummy-session-':'only-key']);
  });
  store.configure('c1',config,secret);const result=await run(store,envelope(),t=>chunks.push(t));
  assert.equal(result.status,'completed');assert.equal(chunks.join(''),'[REDACTED]');assert.ok(!JSON.stringify(result).includes(secret));store.close();
});

test('EOF without done terminates as failure; done followed by stalled iterator does not hang',async()=>{
  const empty=createAssistantMessageEventStream();empty.end(message());
  const eof=await collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport:()=>empty}));
  assert.equal(eof.result.errorMessage,'MODEL_INCOMPLETE_STREAM');
  const done=await collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport:()=>stream()}));
  assert.equal(done.result.stopReason,'stop');
});

test('output tokens and event byte ceilings fail closed',async()=>{
  for(const m of [Object.assign(message(),{usage:{...message().usage,output:257}}),message('x'.repeat(513000))]){
    const result=await collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport:()=>stream(m)}));
    assert.equal(result.result.errorMessage,'MODEL_OUTPUT_LIMIT');
  }
});

test('host timeout and cancellation resolve stalled transports and do not retry',async()=>{
  let calls=0;const transport:StreamFn=()=>{calls++;return new Promise(()=>{});};
  const controller=new AbortController();controller.abort();const budget=new ModelCallBudget(1);
  const pre=await collect(createProviderStream({config,credential:secret,budget,transport}),controller.signal);
  assert.equal(pre.result.errorMessage,'MODEL_CANCELLED');assert.equal(calls,0);assert.equal(budget.calls,0);
  const active=new AbortController();const pending=collect(createProviderStream({config,credential:secret,budget,transport}),active.signal);
  setTimeout(()=>active.abort(),10);assert.equal((await pending).result.errorMessage,'MODEL_CANCELLED');
  const timeout=await collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport}));
  assert.equal(timeout.result.errorMessage,'MODEL_TIMEOUT');assert.equal(calls,2);
});

test('real Pi loop with fake transport verifies readonly action, streaming, history and cleanup',async()=>{
  const contexts:unknown[]=[];const store=new ModelSessions((m,c,o)=>{contexts.push(structuredClone(c));return offlineModel(m,c,o);});
  store.configure('c1',config,secret);const chunks:string[]=[];
  const first=await run(store,envelope('first','c1','Remember sample request'),t=>chunks.push(t));
  assert.equal(first.status,'completed');assert.equal(first.records[0]?.status,'succeeded');
  assert.equal(first.records[0]?.output?.evidence[0]?.kind,'file-sha256');assert.ok(chunks.join('').length>0);
  const second=await run(store,envelope('second','c1','Follow up'));
  assert.equal(second.status,'completed');assert.match(JSON.stringify(contexts[2]),/Remember sample request/);
  assert.equal(store.status('c1').calls,4);assert.equal(store.status('c1').cost,'unknown');
  assert.ok(!JSON.stringify([contexts,first,second,store.status('c1')]).includes(secret));
  assert.equal((await run(store,envelope('third'))).status,'failed');store.close();
});

test('conversations isolate credentials and histories; active configuration is immutable',async()=>{
  const seen:string[]=[];const contexts:unknown[]=[];
  const store=new ModelSessions((_m,c,o)=>{seen.push(o!.apiKey!);contexts.push(c);return stream();});
  store.configure('c1',config,secret);store.configure('c2',config,'dummy-second-key');
  const execution=await store.create(envelope(),()=>{});
  assert.throws(()=>store.configure('c1',config,secret),/BUSY/);assert.throws(()=>store.clear('c1'),/BUSY/);
  await execution.cleanup();await run(store,envelope('first','c1','private-first-text'));await run(store,envelope('second','c2'));
  assert.deepEqual(seen,[secret,'dummy-second-key']);assert.ok(!JSON.stringify(contexts[1]).includes('private-first-text'));
  assert.deepEqual(store.status('missing'),{configured:false});store.clear('c1');await assert.rejects(store.create(envelope(),()=>{}),/NOT_CONFIGURED/);
  store.close();assert.deepEqual(store.status('c2'),{configured:false});assert.throws(()=>store.configure('c1',config,secret));
});

test('session envelope lifecycle exposes no key, correlates stream/result and cancels on disconnect',async()=>{
  const store=new ModelSessions(offlineModel);store.configure('c1',config,secret);
  const events:SessionEnvelope[]=[];const session=new FixtureSession({mode:'pi',create:(e,t)=>store.create(e,t),send:e=>events.push(e)});
  session.receive(envelope());await session.idle();
  assert.equal(events.at(-1)?.payload.status,'completed');assert.ok(events.some(e=>e.type==='server:message_delta'));
  assert.ok(events.every(e=>e.requestId==='r1'&&e.conversationId==='c1'));assert.ok(!JSON.stringify(events).includes(secret));
  await session.close();store.close();
  let started!:()=>void;const start=new Promise<void>(resolve=>started=resolve);
  const slow=new ModelSessions(()=>{started();return new Promise(()=>{});});slow.configure('c1',config,secret);
  const closing=new FixtureSession({mode:'pi',create:(e,t)=>slow.create(e,t),send:()=>{}});closing.receive(envelope());await start;await closing.close();slow.close();
});

test('readonly sample rejects traversal, extra args and hard links to other files',async()=>{
  const root=await realpath(await mkdtemp(join(tmpdir(),'openeagle-readonly-test-')));
  try {
    await writeFile(join(root,'sample.txt'),'fixture');const tools=await readonlyWorkspace(root);
    const context={id:'t',goal:'read',workspace:root,facts:[]};const signal=new AbortController().signal;
    const read=tools.find(t=>t.name==='workspace_read')!;
    for(const args of [{path:'../outside'},{path:join(root,'sample.txt')},{path:'sample.txt',extra:1}])await assert.rejects(read.prepare(args as import('../src/contracts.js').Json,context,signal));
    const prepared=await read.prepare({path:'sample.txt'},context,signal);const value=await prepared.execute(signal);assert.equal(await prepared.verify(value,signal),true);
    await link(join(root,'sample.txt'),join(root,'other.txt'));await assert.rejects(read.prepare({path:'sample.txt'},context,signal),/REJECTED/);
  }finally{await rm(root,{recursive:true,force:true});}
});

test('unknown/missing cost is never reported as zero; only complete validated usage is estimated',()=>{
  const usage={input:1000,output:500,cacheRead:0,cacheWrite:0};const prices={input:1,output:2,cacheRead:0,cacheWrite:0};
  assert.equal(estimateCost(usage,true,undefined).kind,'unknown');assert.equal(estimateCost(usage,false,prices).kind,'unknown');
  assert.equal(estimateCost(undefined,true,prices).kind,'unknown');assert.deepEqual(estimateCost(usage,true,prices),{kind:'estimated',usd:0.002});
  assert.equal(estimateCost(usage,true,{...prices,input:NaN}).kind,'unknown');
});

test('installed provider SDKs use injected fetch for all three explicit APIs (no network)',async()=>{
  const fixtures=[
    {provider:'openai-like',api:'openai-completions',body:'data: '+JSON.stringify({id:'chat1',object:'chat.completion.chunk',created:0,model:'offline-test',choices:[{index:0,delta:{role:'assistant',content:'ok'},finish_reason:null}]})+'\n\ndata: '+JSON.stringify({id:'chat1',object:'chat.completion.chunk',created:0,model:'offline-test',choices:[{index:0,delta:{},finish_reason:'stop'}],usage:{prompt_tokens:2,completion_tokens:1,total_tokens:3}})+'\n\ndata: [DONE]\n\n'},
    {provider:'openai',api:'openai-responses',body:[
      {type:'response.created',response:{id:'resp1',object:'response',status:'in_progress',output:[]}},
      {type:'response.output_item.added',output_index:0,item:{type:'message',id:'msg1',role:'assistant',status:'in_progress',content:[]}},
      {type:'response.content_part.added',item_id:'msg1',output_index:0,content_index:0,part:{type:'output_text',text:'',annotations:[]}},
      {type:'response.output_text.delta',item_id:'msg1',output_index:0,content_index:0,delta:'ok'},
      {type:'response.output_item.done',output_index:0,item:{type:'message',id:'msg1',role:'assistant',status:'completed',content:[{type:'output_text',text:'ok',annotations:[]}]}},
      {type:'response.completed',response:{id:'resp1',status:'completed',output:[],usage:{input_tokens:2,output_tokens:1,total_tokens:3}}}
    ].map(e=>'event: '+e.type+'\ndata: '+JSON.stringify(e)+'\n\n').join('')},
    {provider:'anthropic',api:'anthropic-messages',body:[
      {type:'message_start',message:{id:'msg1',type:'message',role:'assistant',model:'offline-test',content:[],stop_reason:null,usage:{input_tokens:2,output_tokens:0}}},
      {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
      {type:'content_block_delta',index:0,delta:{type:'text_delta',text:'ok'}},
      {type:'content_block_stop',index:0},
      {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:1}},
      {type:'message_stop'}
    ].map(e=>'event: '+e.type+'\ndata: '+JSON.stringify(e)+'\n\n').join('')},
  ];
  for(const fixture of fixtures){
    let calls=0;const c=parseModelConfig({...config,provider:fixture.provider,api:fixture.api});
    const fn=createProviderStream({config:c,credential:secret,budget:new ModelCallBudget(1),fetch:async(resource,init)=>{
      calls++;assert.ok(String(resource).startsWith('https://fixture.invalid/v1/'));assert.equal(init?.redirect,'error');
      const headers=new Headers(init?.headers);assert.ok(headers.get('authorization')===`Bearer ${secret}`||headers.get('x-api-key')===secret);
      assert.equal(JSON.parse(String(init?.body)).model,'offline-test');
      return new Response(fixture.body,{headers:{'content-type':'text/event-stream'}});
    }});
    const source=await fn(configuredModel(c),{messages:[{role:'user',content:'hello',timestamp:0}]} as unknown as TranscriptContext,{});
    for await(const _event of source){/* drain SDK parser */}
    const result=await source.result();assert.equal(result.stopReason,'stop',fixture.api);assert.equal(calls,1,fixture.api);
    assert.deepEqual(result.content,[{type:'text',text:'ok'}],fixture.api);
  }
});

test('cancel during iterator wait and raw provider errors are bounded and sanitized',async()=>{
  const abort=new AbortController();const stalled=createAssistantMessageEventStream();
  const pending=collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport:()=>stalled}),abort.signal);
  setTimeout(()=>abort.abort(),10);assert.equal((await pending).result.errorMessage,'MODEL_CANCELLED');
  const m=message(secret,'error');m.errorMessage=secret;
  const result=await collect(createProviderStream({config,credential:secret,budget:new ModelCallBudget(1),transport:()=>stream(m)}));
  assert.equal(result.result.errorMessage,'MODEL_PROVIDER_FAILED');assert.ok(!JSON.stringify(result).includes(secret));
});
