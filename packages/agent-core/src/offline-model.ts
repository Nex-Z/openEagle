import {createAssistantMessageEventStream,type AssistantMessage} from '@earendil-works/pi-ai';
import type {StreamFn} from '@earendil-works/pi-agent-core';
// Selected only by a host launch flag for offline UI verification; never by renderer config.
export const offlineModel:StreamFn=(model,context)=>{
  const stream=createAssistantMessageEventStream();
  const tool=context.messages.some(m=>m.role==='toolResult');
  const content:AssistantMessage['content']=tool?[{type:'text',text:'离线 Pi 验收：已通过统一 runtime 读取临时 sample.txt。'}]:[{type:'toolCall',id:'sample-read',name:'workspace_read',arguments:{path:'sample.txt'}}];
  const message:AssistantMessage={role:'assistant',content,api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),stopReason:tool?'stop':'toolUse',usage:{input:10,output:12,cacheRead:0,cacheWrite:0,totalTokens:22,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
  if(tool)stream.push({type:'text_delta',contentIndex:0,delta:(content[0] as {text:string}).text,partial:message});
  stream.push({type:'done',reason:tool?'stop':'toolUse',message});return stream;
};
