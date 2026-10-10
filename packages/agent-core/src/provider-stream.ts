import { createAssistantMessageEventStream, type AssistantMessage, type Api, type Model } from '@earendil-works/pi-ai';
import type { StreamFn } from '@earendil-works/pi-agent-core';
import type { ModelConfig } from './model-config.js';

export class ModelCallBudget {
  calls=0;
  constructor(readonly maxCalls:number){}
  reserve(){if(this.calls>=this.maxCalls)throw new Error('MODEL_CALL_BUDGET_EXHAUSTED');this.calls++;}
}
export function sanitize(value: unknown, secret:string):unknown {
  if(typeof value==='string')return value.split(secret).join('[REDACTED]');
  if(Array.isArray(value))return value.map(v=>sanitize(v,secret));
  if(value&&typeof value==='object'){
    const record=value as Record<string,unknown>;
    if(record.role==='assistant'&&Array.isArray(record.content)){
      const text=record.content.filter(p=>p?.type==='text').map(p=>p.text).join('');
      const other=record.content.filter(p=>p?.type!=='text');
      return Object.fromEntries(Object.entries({...record,content:[...(text?[{type:'text',text:text.split(secret).join('[REDACTED]')}]:[]),...other]}).map(([k,v])=>[k.split(secret).join('[REDACTED]'),sanitize(v,secret)]));
    }
    return Object.fromEntries(Object.entries(value).map(([k,v])=>[k.split(secret).join('[REDACTED]'),sanitize(v,secret)]));
  }
  return value;
}
class ChunkRedactor {
  private tail='';
  constructor(private readonly secret:string){}
  push(chunk:string,final=false):string {
    let text=(this.tail+chunk).split(this.secret).join('[REDACTED]');this.tail='';
    if(!final){for(let n=Math.min(this.secret.length-1,text.length);n>0;n--){if(text.endsWith(this.secret.slice(0,n))){this.tail=text.slice(-n);text=text.slice(0,-n);break;}}}
    return text;
  }
}
function failure(model:Model<Api>,code:string):AssistantMessage {
  return {role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),stopReason:'error',errorMessage:code,
    usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
}
function interrupted<T>(promise:Promise<T>,signal:AbortSignal):Promise<T>{
  if(signal.aborted){void promise.catch(()=>{});return Promise.reject(new Error('MODEL_CANCELLED'));}
  return new Promise((resolve,reject)=>{const abort=()=>reject(new Error('MODEL_CANCELLED'));signal.addEventListener('abort',abort,{once:true});promise.then(resolve,reject).finally(()=>signal.removeEventListener('abort',abort)).catch(()=>{});});
}
// Credentials stay in this request-scoped closure. No environment/auth-file lookup,
// no raw provider error/body logging, and no credential-bearing telemetry callbacks.
export function createProviderStream(input:{config:Readonly<ModelConfig>;credential:string;budget:ModelCallBudget;transport?:StreamFn;fetch?:typeof fetch;onText?:(text:string)=>void}):StreamFn {
  // One redactor spans all model turns in this task. Never flush a secret prefix at a turn boundary.
  const redactor=new ChunkRedactor(input.credential);
  return (model,context,options)=>{
    const output=createAssistantMessageEventStream();
    const controller=new AbortController();
    const signal=AbortSignal.any([...(options?.signal?[options.signal]:[]),controller.signal]);
    let timedOut=false;
    const timer=setTimeout(()=>{timedOut=true;controller.abort();},input.config.timeoutMs);
    void (async()=>{
      let terminal=false,eventBytes=0,eventCount=0;
      try {
        signal.throwIfAborted();input.budget.reserve();
        if(JSON.stringify(context).length>64000)throw new Error('MODEL_INPUT_LIMIT');
        const approved=new URL(input.config.endpoint);
        const guardedFetch:typeof fetch=async(resource,init)=>{
          const target=new URL(typeof resource==='string'?resource:resource instanceof URL?resource.href:resource.url);
          if(target.origin!==approved.origin||target.username||target.password||!(target.pathname===approved.pathname||target.pathname.startsWith(approved.pathname.replace(/\/$/,'')+'/')))throw new Error('MODEL_ENDPOINT_BLOCKED');
          const response=await (input.fetch??globalThis.fetch)(resource,{...init,signal,redirect:'error'});
          if(!response.body)return response;
          let bytes=0;
          return new Response(response.body.pipeThrough(new TransformStream<Uint8Array,Uint8Array>({transform(chunk,controller){bytes+=chunk.byteLength;if(bytes>512_000)throw new Error('MODEL_OUTPUT_LIMIT');controller.enqueue(chunk);}})),{status:response.status,statusText:response.statusText,headers:response.headers});
        };
        const transport=input.transport??(await import('@earendil-works/pi-ai/compat')).streamSimple;
        const raw=await interrupted(Promise.resolve(transport(model,context,{signal,apiKey:input.credential,fetch:guardedFetch,
          maxTokens:input.config.maxOutputTokens,timeoutMs:input.config.timeoutMs,maxRetries:0,transport:'sse',cacheRetention:'none',env:{}})),signal);
        const iterator=raw[Symbol.asyncIterator]();
        while(true){
          const next=await interrupted(iterator.next(),signal);if(next.done)break;
          const event=next.value;
          eventBytes+=Buffer.byteLength(JSON.stringify(event));
          if(++eventCount>4096||eventBytes>512_000)throw new Error('MODEL_OUTPUT_LIMIT');
          if(event.type==='error')throw new Error('MODEL_PROVIDER_FAILED');
          const safe=sanitize(event,input.credential) as typeof event;
          if(event.type==='text_delta'){
            const delta=redactor.push(event.delta);input.onText?.(delta);
            // A delta may split the credential. Only forward the redacted incremental stream.
            (safe as typeof event).delta=delta;
          }
          if(event.type==='done'){
            terminal=true;
            if(event.message.usage?.output>input.config.maxOutputTokens)throw new Error('MODEL_OUTPUT_LIMIT');
            // A trailing credential prefix stays withheld; the final answer is emitted separately.
          }
          output.push(safe);
          if(terminal)break;
        }
        if(!terminal)throw new Error("MODEL_INCOMPLETE_STREAM");
      } catch(error) {
        controller.abort();
        const known=error instanceof Error&&['MODEL_CALL_BUDGET_EXHAUSTED','MODEL_INPUT_LIMIT','MODEL_ENDPOINT_BLOCKED','MODEL_OUTPUT_LIMIT','MODEL_INCOMPLETE_STREAM'].includes(error.message)?error.message:timedOut?'MODEL_TIMEOUT':options?.signal?.aborted?'MODEL_CANCELLED':'MODEL_PROVIDER_FAILED';
        output.push({type:'error',reason:'error',error:failure(model,known)});
      } finally {clearTimeout(timer);output.end();}
    })();
    return output;
  };
}
