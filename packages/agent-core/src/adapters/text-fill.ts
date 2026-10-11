import type {Capability,Json} from '../contracts.js';
/** Text replacement is a semantic effect, not a keyboard action. Only installed host backends are exposed. */
export function createTextFillCapability(browserFill:Capability):Capability {
 if(browserFill.name!=='browser_fill')throw new Error('TEXT_FILL_BACKEND_REJECTED');
 return {
  name:'text_fill',description:'受控本地 fixture：Replace the whole value of an observed browser element with text, including Unicode and whitespace. Uses the existing isolated loopback browser semantic fill, never OS keys or clipboard. Desktop controls and native text insertion are not available; no implicit fallback.',
  interaction:{interactionMode:'background',usesSystemInput:false,mayChangeFocus:false,cancelSupport:'browser-session-close'},
  availability:browserFill.availability,
  inputSchema:{type:'object',properties:{target:{type:'object',properties:{kind:{type:'string',enum:['browser-element']},ref:{type:'string',minLength:1}},required:['kind','ref'],additionalProperties:false},text:{type:'string',maxLength:10000}},required:['target','text'],additionalProperties:false},
  async prepare(args,context,signal){
   if(!args||typeof args!=='object'||Array.isArray(args))throw new Error('TEXT_FILL_ARGUMENT_REJECTED');
   const target=args.target;
   if(!target||typeof target!=='object'||Array.isArray(target)||target.kind!=='browser-element'||typeof target.ref!=='string'||!target.ref||typeof args.text!=='string'||args.text.length>10000||Object.keys(args).some(k=>!['target','text'].includes(k))||Object.keys(target).some(k=>!['kind','ref'].includes(k)))throw new Error('TEXT_FILL_TARGET_OR_TEXT_UNSUPPORTED');
   const prepared=await browserFill.prepare({ref:target.ref,value:args.text},context,signal);
   return {...prepared,async execute(executionSignal){const output=await prepared.execute(executionSignal);return {...output,evidence:[...output.evidence,{kind:'text-method',source:'text_fill',value:{operation:'replace',mechanism:'browser-semantic-fill',usesSystemInput:false} as Json}]};},verify:(output,verificationSignal)=>prepared.verify(output,verificationSignal)};
  },
 };
}
