import {acquireDesktopLease} from '../desktop-lease.js';
import {spawn,type ChildProcess} from 'node:child_process';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import type {Capability,Json,ActionOutput} from '../contracts.js';
export interface UiaTarget {hwnd:string;processId:number;startTicks?:string;rootId?:string}
export interface UiaSnapshot {automationId:string;elementId:string;identityHash:string;valueHash:string;fingerprint:string}
export interface WindowsUiaOptions {
 /** Trusted host only. Executable, HWND and process selection are never model arguments. */
 command:{executable:string;args?:readonly string[]};target:UiaTarget;allowedAutomationIds:readonly string[];
 timeoutMs?:number;observationTtlMs?:number;
 /** Enable only after this target/provider passes an independent background test. */
 backgroundVerified?:boolean;
}
export class UiaError extends Error {constructor(code:string,readonly mayHaveChanged=false){super(code);this.name='UiaError';}}
function object(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))throw new UiaError('UIA_PROTOCOL_ERROR');return value as Record<string,unknown>;}
function digest(text:string){return createHash('sha256').update(text).digest('hex');}
function validText(value:unknown):value is string {return typeof value==='string'&&value.length<=10000&&!value.includes('\0')&&!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value);}
function snapshot(value:unknown):UiaSnapshot {const p=object(value);if(typeof p.automationId!=='string'||typeof p.elementId!=='string'||!['identityHash','valueHash','fingerprint'].every(k=>typeof p[k]==='string'&&/^[a-f0-9]{64}$/.test(p[k] as string)))throw new UiaError('UIA_PROTOCOL_ERROR');return p as unknown as UiaSnapshot;}
/** One worker per operation. Blocked UIA providers cannot block Electron's event loop. */
export class UiaProcessClient {
 private active=new Set<ChildProcess>();private closed=false;
 constructor(private readonly command:WindowsUiaOptions['command'],private readonly timeoutMs=4000){if(!path.isAbsolute(command.executable)||!Number.isFinite(timeoutMs)||timeoutMs<50||timeoutMs>30000)throw new UiaError('UIA_HOST_CONFIG_REJECTED');}
 async request(operation:'bind'|'inspect'|'set',payload:Record<string,unknown>,signal?:AbortSignal):Promise<unknown>{
  signal?.throwIfAborted();if(this.closed||this.active.size)throw new UiaError('UIA_CLIENT_UNAVAILABLE');const requestId=randomUUID();const input=JSON.stringify({...payload,version:1,requestId,operation});if(Buffer.byteLength(input)>65536)throw new UiaError('UIA_REQUEST_TOO_LARGE');
  return new Promise((resolve,reject)=>{
   const child=spawn(this.command.executable,[...(this.command.args??[])],{windowsHide:true,stdio:['pipe','pipe','pipe'],cwd:path.dirname(this.command.executable),env:{SystemRoot:process.env.SystemRoot??'',WINDIR:process.env.WINDIR??'',TEMP:process.env.TEMP??'',TMP:process.env.TMP??'',DOTNET_CLI_TELEMETRY_OPTOUT:'1',DOTNET_GENERATE_ASPNET_CERTIFICATE:'false'}});this.active.add(child);
   const chunks:Buffer[]=[];let size=0,stopped:UiaError|undefined,settled=false;let cleanupTimer:ReturnType<typeof setTimeout>|undefined;
   const finish=(error?:Error,result?:unknown)=>{if(settled)return;settled=true;clearTimeout(timer);if(cleanupTimer)clearTimeout(cleanupTimer);signal?.removeEventListener('abort',abort);if(error)reject(error);else resolve(result);};
   const stop=(code:string)=>{if(stopped)return;stopped=new UiaError(code,operation==='set');child.kill('SIGKILL');cleanupTimer=setTimeout(()=>finish(new UiaError('UIA_CLEANUP_INCOMPLETE',operation==='set')),2000);};
   const abort=()=>stop('UIA_CANCELLED');const timer=setTimeout(()=>stop('UIA_TIMEOUT'),this.timeoutMs);signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)abort();
   child.stdout.on('data',chunk=>{size+=chunk.length;if(size>131072){stop('UIA_RESPONSE_TOO_LARGE');return;}chunks.push(chunk);});child.stderr.resume();
   child.once('error',()=>{this.active.delete(child);finish(new UiaError('UIA_LAUNCH_FAILED'));});
   child.once('close',code=>{this.active.delete(child);if(stopped){finish(stopped);return;}try{const stdout=Buffer.concat(chunks).toString('utf8');if(code!==0||stdout.trim().split(/\r?\n/).length!==1)throw new UiaError('UIA_PROTOCOL_ERROR',operation==='set');const response=object(JSON.parse(stdout));if(response.version!==1||response.requestId!==requestId||typeof response.ok!=='boolean')throw new UiaError('UIA_PROTOCOL_ERROR',operation==='set');if(!response.ok){const error=object(response.error);if(typeof error.code!=='string'||!/^[A-Z0-9_]{1,80}$/.test(error.code)||typeof error.mayHaveChanged!=='boolean')throw new UiaError('UIA_PROTOCOL_ERROR',operation==='set');throw new UiaError(error.code,error.mayHaveChanged);}finish(undefined,response.result);}catch(error){finish(error instanceof UiaError?error:new UiaError('UIA_PROTOCOL_ERROR',operation==='set'));}});
   child.stdin.on('error',()=>stop('UIA_PIPE_FAILED'));child.stdin.end(input+'\n');
  });
 }
 async close(){this.closed=true;await Promise.all([...this.active].map(child=>new Promise<void>((resolve,reject)=>{const timer=setTimeout(()=>reject(new UiaError('UIA_CLEANUP_INCOMPLETE',true)),2000);child.once('close',()=>{clearTimeout(timer);resolve();});child.kill('SIGKILL');})));}
}
export async function createWindowsUiaCapabilities(options:WindowsUiaOptions,signal?:AbortSignal):Promise<{capabilities:Capability[];close():Promise<void>}> {
 if(process.platform!=='win32')throw new UiaError('UIA_WINDOWS_ONLY');
 const client=new UiaProcessClient(options.command,options.timeoutMs);const release=await acquireDesktopLease();
 try{const session=await createBoundUiaCapabilities(client,options,signal);return {capabilities:session.capabilities,async close(){await session.close();await release();}};}catch(error){await client.close();await release();throw error;}
}
/** Injectable transport is only an offline test seam, not evidence of OS UIA. */
export async function createBoundUiaCapabilities(client:Pick<UiaProcessClient,'request'|'close'>,options:WindowsUiaOptions,signal?:AbortSignal){
 const ids=[...options.allowedAutomationIds];if(ids.length<1||ids.length>16||new Set(ids).size!==ids.length||ids.some(id=>!id||id.length>128))throw new UiaError('UIA_HOST_CONFIG_REJECTED');
 const target=object(await client.request('bind',{target:options.target},signal)) as unknown as UiaTarget;
 if(target.hwnd!==options.target.hwnd||target.processId!==options.target.processId||typeof target.startTicks!=='string'||!/^\d+$/.test(target.startTicks)||typeof target.rootId!=='string'||!target.rootId)throw new UiaError('UIA_BINDING_REJECTED');
 Object.freeze(target);let closed=false,poisoned=false;const observations=new Map<string,{state:UiaSnapshot;at:number}>();const ttl=options.observationTtlMs??2500;if(!Number.isFinite(ttl)||ttl<1||ttl>10000)throw new UiaError('UIA_HOST_CONFIG_REJECTED');
 const active=()=>{if(closed||poisoned)throw new UiaError('UIA_SESSION_STOPPED');};
 const read=async(id:string,signal?:AbortSignal)=>{active();const state=snapshot(await client.request('inspect',{target,automationId:id},signal));if(state.automationId!==id)throw new UiaError('UIA_PROTOCOL_ERROR');return state;};
 const resources=['desktop:uia:'+target.processId+':'+target.hwnd,...(!options.backgroundVerified?['desktop:system-input']:[])];
 const interaction={interactionMode:options.backgroundVerified?'background' as const:'unknown' as const,usesSystemInput:false,mayChangeFocus:!options.backgroundVerified,cancelSupport:'worker termination; in-flight provider effects may remain unknown',targetWindow:target.hwnd};
 const capabilities:Capability[]=[{
  name:'uia_observe',description:'Observe one host-allowed AutomationId inside a PID/start-time/HWND/runtime-ID bound Windows window. Reject ambiguous, password, readonly, disabled or unsupported controls; return hashes, not field text. No desktop-wide search.',interaction,availability:{supported:true},
  inputSchema:{type:'object',properties:{automationId:{type:'string',enum:ids}},required:['automationId'],additionalProperties:false},
  async prepare(args,_context,signal){active();const p=object(args);if(typeof p.automationId!=='string'||!ids.includes(p.automationId)||Object.keys(p).length!==1)throw new UiaError('UIA_SELECTOR_REJECTED');const id=p.automationId;signal?.throwIfAborted();let output:ActionOutput|undefined;
   return {target:target.hwnd,resources,effects:['read'],async execute(signal){const state=await read(id,signal);if(observations.size>=64)observations.delete(observations.keys().next().value!);const observationId=randomUUID();observations.set(observationId,{state,at:performance.now()});output={value:{observationId,...state} as unknown as Json,evidence:[{kind:'uia-observation',source:'FlaUI.UIA3',value:{identityHash:state.identityHash,valueHash:state.valueHash}}]};return output;},async verify(value){return value===output;}};
  },
 },{
  name:'uia_set_text',description:'Replace the whole value with UIA ValuePattern.SetValue, not keys or clipboard. Requires an observed host-allowed control and independent readback. No fallback/retry; provider background behavior is not assumed.',interaction,availability:{supported:true},
  inputSchema:{type:'object',properties:{observationId:{type:'string'},text:{type:'string',maxLength:10000}},required:['observationId','text'],additionalProperties:false},
  async prepare(args,_context,signal){active();const p=object(args);if(typeof p.observationId!=='string'||!validText(p.text)||Object.keys(p).some(k=>!['observationId','text'].includes(k)))throw new UiaError('UIA_TEXT_REJECTED');const id=p.observationId,text=p.text,observation=observations.get(id);if(!observation||performance.now()-observation.at>ttl)throw new UiaError('UIA_STALE_OBSERVATION');const state=observation.state;const current=await read(state.automationId,signal);if(current.fingerprint!==state.fingerprint)throw new UiaError('UIA_TARGET_CHANGED');let used=false,output:ActionOutput|undefined;
   return {target:target.hwnd+'/'+state.automationId,fingerprint:state.fingerprint,resources,effects:['write','external-submit'],async execute(signal){active();signal.throwIfAborted();if(used||observations.get(id)!==observation||performance.now()-observation.at>ttl)throw new UiaError('UIA_REPLAY_OR_STALE');used=true;observations.clear();try{const result=snapshot(await client.request('set',{target,automationId:state.automationId,expectedFingerprint:state.fingerprint,text},signal));if(result.identityHash!==state.identityHash||result.valueHash!==digest(text))throw new UiaError('UIA_VALUE_MISMATCH',true);output={value:{applied:true,mechanism:'uia-value-pattern',identityHash:result.identityHash,valueHash:result.valueHash},evidence:[{kind:'uia-provider-receipt',source:'FlaUI.UIA3',value:{identityHash:result.identityHash,valueHash:result.valueHash}}]};return output;}catch(error){poisoned=true;throw error;}},async verify(value,signal){try{if(value!==output)throw new UiaError('UIA_VERIFY_REJECTED');const after=await read(state.automationId,signal);const verified=after.identityHash===state.identityHash&&after.valueHash===digest(text);if(!verified)poisoned=true;return verified;}catch(error){poisoned=true;throw error;}}};
  },
 }];
 return {capabilities,async close(){closed=true;observations.clear();await client.close();}};
}