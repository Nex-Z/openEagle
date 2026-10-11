import type {Capability,Json,ActionOutput} from '../contracts.js';
import {DesktopCapabilityClient} from '../reused-desktop-client.js';
export const GUI_INTERACTION={interactionMode:'foreground',usesSystemInput:true,mayChangeFocus:true,cancelSupport:'cooperative-between-inputs',targetWindow:'host-authorized-window'} as const;
// 复用 desktop-tools 的 capability/client/ticket 契约；不包含 fixture 控件或业务文本。
export function desktopCapabilities(client:DesktopCapabilityClient,requestId:string,tickets:Map<string,string>,target:string):Capability[]{
  return ['desktop_observe','desktop_click','desktop_type'].map(name=>({
    name,interaction:name==='desktop_observe'?{...GUI_INTERACTION,usesSystemInput:false,mayChangeFocus:false}:GUI_INTERACTION,
    description:name+': host-authorized visible window; foreground lease required; no automatic retry; click coordinates are content DIP pixels',availability:{supported:true},
    inputSchema:name==='desktop_observe'?{type:'object',properties:{},additionalProperties:false}:{type:'object',properties:{target:{type:'string'},observationId:{type:'string'},...(name==='desktop_click'?{x:{type:'number',minimum:0},y:{type:'number',minimum:0}}:{text:{type:'string',minLength:1,maxLength:80}})},required:['target','observationId',...(name==='desktop_click'?['x','y']:['text'])],additionalProperties:false},
    async prepare(args,_context,signal){
      if(!args||typeof args!=='object'||Array.isArray(args))throw new Error('GUI_ARGUMENT_REJECTED');
      const payload=args as Record<string,Json>;const observe=name==='desktop_observe';
      if(!observe&&payload.target!==target)throw new Error('GUI_TARGET_MISMATCH');
      const inspected=observe?undefined:await client.request(requestId,'screen.capture',{operation:'inspect',observationId:payload.observationId,target},{signal});
      const key=JSON.stringify({name,args});let output:ActionOutput|undefined;
      return {target,fingerprint:observe?'observe-window':String(inspected!.fingerprint),resources:['desktop:system-input'],effects:observe?['read']:['execute'],
        async execute(signal){
          const ticket=tickets.get(key);tickets.delete(key);
          const value=await client.request(requestId,observe?'screen.capture':name==='desktop_click'?'desktop.mouse':'desktop.keyboard',observe?{operation:'observe'}:payload,{signal,...(ticket?{confirmationId:ticket}:{})});
          output={value:value as Json,evidence:[{kind:observe?'native-window-image':'native-window-receipt',source:'main-capability-broker',value:value as Json}]};return output;
        },
        async verify(value){return value===output&&(observe?typeof (value.value as Record<string,Json>).sha256==='string':(value.value as Record<string,Json>).verified===true);},
      };
    },
  }));
}
