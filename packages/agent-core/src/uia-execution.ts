import {createWindowsUiaCapabilities,type WindowsUiaOptions} from './adapters/windows-uia.js';
import {CapabilityRegistry} from './runtime.js';
import {ScriptedEngine} from './engines/scripted.js';
import type {SessionEnvelope,SessionExecution} from './fixture-session.js';
type Control=(operation:string,payload:Record<string,unknown>)=>Promise<Record<string,unknown>>;
export class UiaSessions {
 constructor(private readonly factory= createWindowsUiaCapabilities){}
 private revisions=new Map<string,number>();
 private active=new Map<string,()=>Promise<void>>();
 async create(e:SessionEnvelope,control:Control):Promise<SessionExecution|undefined>{
  const revision=this.revisions.get(e.conversationId)??0;const requestId=e.conversationId+':'+e.requestId;const binding=await control('uia-claim',{conversationId:e.conversationId,requestId});if(binding.configured!==true)return undefined;
  const token=String(binding.token);if((this.revisions.get(e.conversationId)??0)!==revision){await control('uia-finish',{requestId,token});throw new Error('UIA_REVOKED');}let session:Awaited<ReturnType<typeof createWindowsUiaCapabilities>>|undefined;let closed=false;let closing:Promise<void>|undefined;
  const cleanup=()=>closing??=(async()=>{await session?.close();closed=true;this.active.delete(e.conversationId);await control('uia-finish',{requestId,token});})();
  const controller=new AbortController();const creating=this.factory({...binding as unknown as WindowsUiaOptions,backgroundVerified:false},controller.signal);
  this.active.set(e.conversationId,async()=>{controller.abort();await creating.catch(()=>{});await cleanup();});
  try{session=await creating;controller.signal.throwIfAborted();const registry=new CapabilityRegistry();for(const capability of session.capabilities)registry.register(capability);
   return {workspace:'.',registry,engine:new ScriptedEngine(async()=>{throw new Error('UIA_REQUIRES_PI_ENGINE');}),authorize:async(request,signal)=>{signal.throwIfAborted();const result=await control('uia-authorize',{requestId,token,args:request.args});signal.throwIfAborted();return !closed&&result.approved===true;},cleanup,report:()=>({uia:true,interactionMode:'unknown',usesSystemInput:false,backgroundVerified:false})};
  }catch(error){await cleanup();throw error;}
 }
 async revoke(conversationId:string){this.revisions.set(conversationId,(this.revisions.get(conversationId)??0)+1);await this.active.get(conversationId)?.();return {revoked:true};}
}