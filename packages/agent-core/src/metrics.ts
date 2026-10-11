export type Phase = 'model'|'prepare'|'lock'|'approval'|'tool'|'verify'|'cleanup';
export interface MetricInput {
  kind: 'span-start'|'span-end'|'tool-call'|'model-call'|'usage'|'retry'|'fallback'|'replan'|'repeated-observation'|'invalid-action'|'human-takeover'|'handoff';
  spanId?: string; phase?: Phase; stepId?: string; attemptId?: string;
  adapter?: string; status?: string;
  usage?: { input: number; output: number; cacheRead: number; cacheWrite: number };
}
export interface MetricEvent extends MetricInput { taskId: string; monotonicMs: number }
export interface Checkpoint { id: string; oracle: 'pass'|'fail'|'unknown'; firstAttempt: 'pass'|'fail'|'unknown' }
export class MetricsCollector {
  readonly events: MetricEvent[] = [];
  readonly collect = (event: MetricEvent) => { this.events.push(structuredClone(event)); };
  jsonl(): string { return this.events.map(e=>JSON.stringify(e)).join('\n'); }
  summary(input: {taskId:string;runtimeStatus:string;taskOracle:'pass'|'fail'|'unknown';checkpoints:Checkpoint[];wallMs:number;recoveryObservationComplete:boolean;modelMode:'scripted'|'simulated'|'real'}) {
    const events=this.events.filter(e=>e.taskId===input.taskId);
    const count=(kind:MetricInput['kind'])=>events.filter(e=>e.kind===kind).length;
    const starts=new Map<string,MetricEvent>(); const phases:Partial<Record<Phase,number>>={}; let incompleteSpans=0;
    for(const e of events){if(e.kind==='span-start'&&e.spanId)starts.set(e.spanId,e);if(e.kind==='span-end'&&e.spanId){const s=starts.get(e.spanId);if(s?.phase){phases[s.phase]=(phases[s.phase]??0)+Math.max(0,e.monotonicMs-s.monotonicMs);starts.delete(e.spanId);}}}
    incompleteSpans=starts.size;
    const recovery={retry:count('retry'),fallback:count('fallback'),replan:count('replan'),repeatedObservation:count('repeated-observation'),invalidAction:count('invalid-action'),humanTakeover:count('human-takeover')};
    const judged=input.checkpoints.filter(c=>c.firstAttempt!=='unknown');
    const adapterStatusCounts:Record<string,number>={};for(const e of events)if(e.kind==='handoff'&&e.status&&e.status!=='requested')adapterStatusCounts[e.status]=(adapterStatusCounts[e.status]??0)+1;
    const usage=events.filter(e=>e.kind==='usage'&&e.usage).map(e=>e.usage!);
    return {schemaVersion:1,...input,phaseResourceMs:phases,phaseSemantics:'resource-time; overlapping spans may exceed wall time',incompleteSpans,
      adapterStatusCounts,calls:{tools:count('tool-call'),model:count('model-call')},recovery,
      cleanSuccess:input.taskOracle==='pass'&&input.recoveryObservationComplete ? Object.values(recovery).every(n=>n===0) : input.taskOracle==='fail'?false:'unknown',
      checkpointFirstCorrect:judged.filter(c=>c.firstAttempt==='pass').length,checkpointJudged:judged.length,checkpointUnknown:input.checkpoints.length-judged.length,firstAttemptCoverage:input.checkpoints.length?judged.length/input.checkpoints.length:null,oracleCoverage:input.checkpoints.length?input.checkpoints.filter(c=>c.oracle!=='unknown').length/input.checkpoints.length:null,
      usage:usage.length?{status:usage.length===count('model-call')?'complete':'partial',source:input.modelMode==='simulated'?'simulated transport':'provider reported',...usage.reduce((a,u)=>({input:a.input+u.input,output:a.output+u.output,cacheRead:a.cacheRead+u.cacheRead,cacheWrite:a.cacheWrite+u.cacheWrite}),{input:0,output:0,cacheRead:0,cacheWrite:0})}:{status:count('model-call')?'unknown':'not-applicable'},
      cost:{status:'unknown',reason:'no explicit versioned pricing source'},
    };
  }
}
export type RunSummary=ReturnType<MetricsCollector['summary']>;
export function summarizeBatch(runs:RunSummary[]) {
  const times=runs.map(r=>r.wallMs).sort((a,b)=>a-b),success=runs.filter(r=>r.taskOracle==='pass').length;
  const judged=runs.reduce((n,r)=>n+r.checkpointJudged,0),correct=runs.reduce((n,r)=>n+r.checkpointFirstCorrect,0),unknown=runs.reduce((n,r)=>n+r.checkpointUnknown,0);
  return {schemaVersion:1,samples:runs.length,successRate:runs.length?success/runs.length:null,taskOracleUnknown:runs.filter(r=>r.taskOracle==='unknown').length,
    cleanSuccessRate:runs.length?runs.filter(r=>r.cleanSuccess===true).length/runs.length:null,cleanSuccessUnknown:runs.filter(r=>r.cleanSuccess==='unknown').length,
    stepFirstCorrectRate:judged?correct/judged:null,firstAttemptCoverage:judged+unknown?judged/(judged+unknown):null,oracleCoverage:judged+unknown?runs.reduce((n,r)=>n+r.checkpoints.filter(c=>c.oracle!=='unknown').length,0)/(judged+unknown):null,checkpointUnknown:unknown,
    latencyMs:{p50:times.length?(times[Math.floor((times.length-1)/2)]!+times[Math.ceil((times.length-1)/2)]!)/2:null,p95:times.length?times[Math.ceil(times.length*.95)-1]:null,min:times[0]??null,max:times.at(-1)??null,interpretation:'exploratory small fixture sample, not a stable performance estimate'},
    callsPerSuccessIncludingFailures:success?{tools:runs.reduce((n,r)=>n+r.calls.tools,0)/success,model:runs.reduce((n,r)=>n+r.calls.model,0)/success}:null,
    costPerSuccess:'unknown',recoveryCost:'unknown',comparison:'old baseline and real model not measured',runs};
}
