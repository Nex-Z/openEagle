import {createAssistantMessageEventStream,type AssistantMessage} from '@earendil-works/pi-ai';
import type {StreamFn} from '@earendil-works/pi-agent-core';
// Selected only by a host launch flag for offline UI verification; never by renderer config.
export const offlineModel:StreamFn=(model,context)=>{
  const stream=createAssistantMessageEventStream();
  if(context.messages.findLast(m=>m.role==='user')?.content==='GUI_REHEARSAL_FOREGROUND'){
    if(!JSON.stringify(context.messages.filter(m=>m.role==='system')).includes('desktop_observe'))throw new Error('GUI_TOOLS_NOT_REGISTERED');
    const results=context.messages.filter(m=>m.role==='toolResult');
    const count=results.length;const latest=results.at(-1);
    const record=latest?JSON.parse(latest.content.filter(c=>c.type==='text').map(c=>c.text).join('')):undefined;
    if(record&&record.status!=='succeeded')throw new Error('GUI_OFFLINE_TOOL_FAILED');
    let content:AssistantMessage['content'];
    if(count>=7)content=[{type:'text',text:'离线 Pi 已调用真实 GUI 工具；固定测试规划，不是 LLM 准确率。'}];
    else if(count%2===0)content=[{type:'toolCall',id:'gui-'+count,name:'desktop_observe',arguments:{}}];
    else {const value=record.output.value;content=[{type:'toolCall',id:'gui-'+count,name:count===3?'desktop_type':'desktop_click',arguments:{target:value.target,observationId:value.observationId,...(count===3?{text:'OpenEagle native input'}:value.state.controls[count===1?'entry':'save'])}}];}
    const done=count>=7;const message:AssistantMessage={role:'assistant',content,api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),stopReason:done?'stop':'toolUse',usage:{input:10,output:12,cacheRead:0,cacheWrite:0,totalTokens:22,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
    stream.push({type:'done',reason:done?'stop':'toolUse',message});return stream;
  }
  const tool=context.messages.some(m=>m.role==='toolResult');
  const content:AssistantMessage['content']=tool?[{type:'text',text:'离线 Pi 验收：已通过统一 runtime 读取临时 sample.txt。'}]:[{type:'toolCall',id:'sample-read',name:'workspace_read',arguments:{path:'sample.txt'}}];
  const message:AssistantMessage={role:'assistant',content,api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),stopReason:tool?'stop':'toolUse',usage:{input:10,output:12,cacheRead:0,cacheWrite:0,totalTokens:22,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
  if(tool)stream.push({type:'text_delta',contentIndex:0,delta:(content[0] as {text:string}).text,partial:message});
  stream.push({type:'done',reason:tool?'stop':'toolUse',message});return stream;
};
