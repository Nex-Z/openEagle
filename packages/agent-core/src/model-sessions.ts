import { mkdtemp, realpath, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { StreamFn } from '@earendil-works/pi-agent-core';
import { PiEngine } from './engines/pi.js';
import { CapabilityRegistry } from './runtime.js';
import { configuredModel, parseCredential, parseModelConfig, type ModelConfig } from './model-config.js';
import { createProviderStream, ModelCallBudget, sanitize } from './provider-stream.js';
import { readonlyWorkspace } from './readonly-workspace.js';
import type { SessionEnvelope, SessionExecution } from './fixture-session.js';
type Configured={config:Readonly<ModelConfig>;credential:string;budget:ModelCallBudget;active:boolean;expires:number;history:Array<{role:'user'|'assistant';content:string}>};
const validId=(v:unknown):v is string=>typeof v==='string'&&/^[a-zA-Z0-9_-]{1,150}$/.test(v);
export class ModelSessions {
  private readonly sessions=new Map<string,Configured>();
  private closed=false;
  constructor(private readonly transport?:StreamFn){}
  configure(conversationId:unknown,config:unknown,credential:unknown){
    if(this.closed||!validId(conversationId))throw new Error('INVALID_MODEL_SESSION');
    if(this.sessions.get(conversationId)?.active)throw new Error('MODEL_SESSION_BUSY');
    if(this.sessions.size>=8&&!this.sessions.has(conversationId))throw new Error('MODEL_SESSION_LIMIT');
    const safe=parseModelConfig(config),secret=parseCredential(credential);
    if(JSON.stringify(safe).includes(secret))throw new Error('CREDENTIAL_IN_PUBLIC_CONFIG');
    this.sessions.set(conversationId,{config:safe,credential:secret,budget:new ModelCallBudget(safe.maxCalls),active:false,history:[],expires:Date.now()+10*60_000});
    return this.status(conversationId);
  }
  status(id:unknown):Record<string,unknown>{
    const current=validId(id)?this.sessions.get(id):undefined;
    if(!current||this.closed||(!current.active&&current.expires<Date.now())){if(validId(id))this.sessions.delete(id);return {configured:false};}
    return {configured:true,provider:current.config.provider,modelId:current.config.modelId,calls:current.budget.calls,maxCalls:current.config.maxCalls,maxOutputTokens:current.config.maxOutputTokens,timeoutMs:current.config.timeoutMs,maxRetries:0,cost:'unknown',workspace:'temporary-sample'};
  }
  clear(id:unknown){if(!validId(id))throw new Error('INVALID_MODEL_SESSION');if(this.sessions.get(id)?.active)throw new Error('MODEL_SESSION_BUSY');this.sessions.delete(id);return {configured:false};}
  async create(e:SessionEnvelope,onText:(text:string)=>void,extend?:()=>Promise<SessionExecution>):Promise<SessionExecution>{
    const current=this.sessions.get(e.conversationId);
    if(!current||this.closed||current.expires<Date.now()){if(current&&!current.active)this.sessions.delete(e.conversationId);throw new Error('MODEL_SESSION_NOT_CONFIGURED');}
    if(current.active)throw new Error('MODEL_SESSION_BUSY');
    let extra:SessionExecution|undefined;const guiSteps:Record<string,unknown>[]=[];const initialCalls=current.budget.calls;
    current.active=true;let workspace:string|undefined,cleaned=false;
    const cleanup=async()=>{if(cleaned)return;cleaned=true;try{try{await extra?.cleanup();}finally{if(workspace)await rm(workspace,{recursive:true,force:true});}}finally{current.active=false;}};
    try {
      workspace=await realpath(await mkdtemp(join(tmpdir(),'openeagle-pi-workspace-')));
      await writeFile(join(workspace,'sample.txt'),'This is an isolated OpenEagle sample.\nTasks: review the sample, report its SHA256-backed observation.\n',{flag:'wx'});
      const registry=new CapabilityRegistry();for(const capability of await readonlyWorkspace(workspace))registry.register(capability);
      extra=await extend?.();if(extra)for(const capability of extra.registry.list())registry.register(capability);
      const streamFn=createProviderStream({config:current.config,credential:current.credential,budget:current.budget,transport:this.transport,onText});
      const pi=new PiEngine({model:configuredModel(current.config),streamFn,maxTurns:current.config.maxCalls,history:current.history});
      return {workspace,registry,engine:{name:pi.name,async run(session){
        const answer=sanitize(await pi.run({...session,invoke:async(name,args)=>{const started=performance.now();const record=await session.invoke(name,args);if(extra)guiSteps.push({name,status:record.status,firstAttempt:record.status==='succeeded',retries:0,durationMs:performance.now()-started});return record;}}),current.credential) as string;
        current.history.push({role:'user',content:sanitize(session.context.goal,current.credential) as string},{role:'assistant',content:answer});
        while(current.history.length>8||JSON.stringify(current.history).length>24000)current.history.splice(0,2);
        return answer;
      }},cleanup,authorize:extra?.authorize,
        report:()=>({...this.status(e.conversationId),...(extra?.report?.()??{}),planning:this.transport?'fake-model':'model',calls:current.budget.calls,modelCalls:current.budget.calls-initialCalls,...(extra?{steps:guiSteps}:{})})};
    } catch(error){await cleanup();throw error;}
  }
  close(){this.closed=true;this.sessions.clear();}
}
