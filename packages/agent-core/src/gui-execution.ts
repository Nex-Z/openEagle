import {createHash} from 'node:crypto';
import {mkdtemp,realpath,rm,writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import type {SessionEnvelope,SessionExecution} from './fixture-session.js';
import type {Json,Authorizer} from './contracts.js';
import {DesktopCapabilityClient,type CapabilityTransport} from './reused-desktop-client.js';
import {desktopCapabilities,GUI_INTERACTION} from './adapters/gui-capabilities.js';
import {CapabilityRegistry} from './runtime.js';
import {ScriptedEngine} from './engines/scripted.js';
type Control=(operation:string,payload:Record<string,unknown>)=>Promise<Record<string,unknown>>;
// Fixed, labelled rehearsal lives in the host integration, not the generic runtime/engine.
export async function createGuiExecution(e:SessionEnvelope,transport:CapabilityTransport,control:Control):Promise<SessionExecution>{
  const foreground=e.payload.content==='GUI_REHEARSAL_FOREGROUND';
  const workspace=await realpath(await mkdtemp(join(tmpdir(),'openeagle-gui-task-')));
  if(!foreground)return {workspace,registry:new CapabilityRegistry(),engine:new ScriptedEngine(async()=> 'needsForeground：此 GUI 操作尚无后台语义能力；未执行、未重试、未抢焦点。可显式请求前台专用窗口验收。'),cleanup:()=>rm(workspace,{recursive:true,force:true}),report:()=>({routing:'needsForeground',reason:'background-capability-unavailable',modelCalls:0,usesSystemInput:false})};
  const wireId=createHash('sha256').update(JSON.stringify([e.conversationId,e.requestId])).digest('hex');let opened=false;
  const client=new DesktopCapabilityClient(transport);const tickets=new Map<string,string>();let closed=false;let report:Record<string,unknown>={interaction:GUI_INTERACTION};
  const cleanup=async()=>{if(closed)return;closed=true;try{if(opened)report={...report,...await control('close',{requestId:wireId,metrics:report})};}finally{client.close();await rm(workspace,{recursive:true,force:true});}};
  try{
    const binding=await control('open',{requestId:wireId});opened=true;
    const registry=new CapabilityRegistry();for(const capability of desktopCapabilities(client,wireId,tickets,String(binding.target)))registry.register(capability);
    const authorize:Authorizer=async(request,signal)=>{
      signal.throwIfAborted();const approved=await control('authorize',{requestId:wireId,capability:request.capability,args:request.args});
      signal.throwIfAborted();tickets.set(JSON.stringify({name:request.capability,args:request.args}),String(approved.ticket));return true;
    };
    const engine=new ScriptedEngine(async session=>{
      session.signal.addEventListener('abort',()=>{void control('cancel',{requestId:wireId}).catch(()=>{});},{once:true});
      const steps:Record<string,unknown>[]=[];report={...report,steps,planning:'scripted',modelCalls:0};
      async function invoke(name:string,args:Json){const started=performance.now();const record=await session.invoke(name,args);steps.push({name,status:record.status,firstAttempt:record.status==='succeeded',retries:0,durationMs:performance.now()-started});if(record.status!=='succeeded')throw new Error(record.error??`GUI_${record.status}`);return record.output!.value as Record<string,Json>;}
      for(const [name,controlName] of [['desktop_click','entry'],['desktop_type','entry'],['desktop_click','save']] as const){
        const observation=await invoke('desktop_observe',{});
        const controls=(observation.state as Record<string,Json>).controls as Record<string,Json>;
        await invoke(name,{target:observation.target!,observationId:observation.observationId!,...(name==='desktop_type'?{text:'OpenEagle native input'}:controls[controlName] as Record<string,Json>)});
      }
      await invoke('desktop_observe',{});report={...report,steps,planning:'scripted',modelCalls:0};
      await writeFile(join(workspace,'metrics.json'),JSON.stringify(report));
      return '真实 GUI 固定流程完成：OS 点击、键盘输入和保存均通过独立窗口状态验证；未调用模型。';
    });
    return {workspace,registry,engine,authorize,cleanup,report:()=>report};
  }catch(error){await cleanup();throw error;}
}
