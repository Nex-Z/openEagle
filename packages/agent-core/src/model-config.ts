import type { Api, Model } from '@earendil-works/pi-ai';

export interface ModelConfig {
  provider: 'openai' | 'openai-like' | 'anthropic';
  api: 'openai-responses' | 'openai-completions' | 'anthropic-messages';
  modelId: string; endpoint: string;
  maxCalls: number; maxOutputTokens: number; timeoutMs: number;
  maxRetries: 0; workspace: 'temporary-sample'; approved: true;
}
const plain=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v);
export function parseModelConfig(value: unknown): Readonly<ModelConfig> {
  if(!plain(value))throw new Error('INVALID_MODEL_CONFIG');
  const allowed=['provider','api','modelId','endpoint','maxCalls','maxOutputTokens','timeoutMs','maxRetries','workspace','approved'];
  if(Object.keys(value).some(k=>!allowed.includes(k)))throw new Error('INVALID_MODEL_CONFIG');
  const expected: Record<string,string>={openai:'openai-responses','openai-like':'openai-completions',anthropic:'anthropic-messages'};
  if(typeof value.provider!=='string'||!Object.hasOwn(expected,value.provider)||value.api!==expected[value.provider])throw new Error('UNSUPPORTED_PROVIDER_API');
  if(typeof value.modelId!=='string'||!/^[a-zA-Z0-9_.:/-]{1,150}$/.test(value.modelId))throw new Error('INVALID_MODEL_ID');
  if(typeof value.endpoint!=='string'||value.endpoint.length>2048)throw new Error('INVALID_MODEL_ENDPOINT');
  let url:URL;try{url=new URL(value.endpoint);}catch{throw new Error('INVALID_MODEL_ENDPOINT');}
  if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash)throw new Error('INVALID_MODEL_ENDPOINT');
  for(const [key,min,max] of [['maxCalls',1,12],['maxOutputTokens',32,2048],['timeoutMs',1000,60000]] as const){
    const n=value[key];if(typeof n!=='number'||!Number.isSafeInteger(n)||n<min||n>max)throw new Error('INVALID_MODEL_BUDGET');
  }
  if(value.maxRetries!==0||value.workspace!=='temporary-sample'||value.approved!==true)throw new Error('MODEL_APPROVAL_REQUIRED');
  return Object.freeze({...value,endpoint:url.href.replace(/\/$/,'')}) as unknown as Readonly<ModelConfig>;
}
// Adapted from old application/pi-model.ts: explicit wire compatibility is kept;
// no catalog fallback, ambient credentials or assumed custom-model prices.
export function configuredModel(config: Readonly<ModelConfig>): Model<Api> {
  return {id:config.modelId,name:config.modelId,api:config.api,provider:config.provider==='openai-like'?'openai':config.provider,
    baseUrl:config.endpoint,reasoning:false,input:['text'],contextWindow:32768,maxTokens:config.maxOutputTokens,
    // Pi requires numeric catalog fields. These placeholders MUST NOT be used for cost reporting.
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0}};
}
export function parseCredential(value:unknown):string {
  if(typeof value!=='string'||value.length<8||value.length>4096||/[\s\x00-\x1f]/.test(value))throw new Error('INVALID_SESSION_CREDENTIAL');
  return value;
}

export type CostEstimate={kind:'unknown';reason:string}|{kind:'estimated';usd:number};
export function estimateCost(usage:{input:number;output:number;cacheRead:number;cacheWrite:number}|undefined,complete:boolean,prices:{input:number;output:number;cacheRead:number;cacheWrite:number}|undefined):CostEstimate{
  if(!prices||!usage||!complete)return {kind:'unknown',reason:'validated prices and complete provider usage are required'};
  const keys=['input','output','cacheRead','cacheWrite'] as const;
  if(keys.some(k=>!Number.isFinite(prices[k])||prices[k]<0||!Number.isSafeInteger(usage[k])||usage[k]<0))return {kind:'unknown',reason:'invalid usage or prices'};
  return {kind:'estimated',usd:keys.reduce((sum,k)=>sum+usage[k]*prices[k]/1_000_000,0)};
}
