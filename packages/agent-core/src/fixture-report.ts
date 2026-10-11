import { performance } from 'node:perf_hooks';
import { mkdtemp, access, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runDemo } from './demo.js';
import { MetricsCollector, summarizeBatch, type RunSummary, type MetricEvent } from './metrics.js';
import { AgentRuntime, CapabilityRegistry } from './runtime.js';
import { ScriptedEngine } from './engines/scripted.js';
import { createWindowsTrustedFileCapabilities } from './adapters/windows-trusted.js';

const runs:RunSummary[]=[]; const trace:MetricEvent[]=[];
for(let i=0;i<3;i++){
  const started=performance.now(), collector=new MetricsCollector();
  try {
    const result=await runDemo({chromiumExecutable:process.env.OPENEAGLE_CHROMIUM,windowsMode:'trusted-workspace',metricsCollector:collector});
    runs.push({...result.metrics,wallMs:performance.now()-started});trace.push(...result.trace);
  }catch(error){
    runs.push(collector.summary({taskId:collector.events[0]?.taskId ?? `fixture-failed-${i}`,runtimeStatus:'failed',taskOracle:'fail',checkpoints:['browser-nonce','server-receipt','file-bytes','cli-hash'].map(id=>({id,oracle:'unknown' as const,firstAttempt:'unknown' as const})),wallMs:performance.now()-started,recoveryObservationComplete:false,modelMode:'scripted'}));
    // 不输出异常中的请求/路径；失败样本必须保留，未捕获的调用开销不能算零。
    trace.push(...collector.events); console.error('fixture failed; retained observed call trace');
  }
}
// 有意拒绝写入：runtime正常结束不代表“生成文件”目标完成。
const root=await mkdtemp(join(tmpdir(),'eagle-metrics-')),collector=new MetricsCollector(),started=performance.now();
try {
  const runtime=new AgentRuntime({registry:new CapabilityRegistry().register(createWindowsTrustedFileCapabilities({mode:'trusted-workspace'})[1]!),onMetric:collector.collect});
  const result=await runtime.start({goal:'创建result.txt',workspace:root},new ScriptedEngine(async s=>{await s.invoke('files_write',{path:'result.txt',content:'fixture'});return 'done';})).result;
  let exists=true;try{await access(join(root,'result.txt'));}catch{exists=false;}
  runs.push(collector.summary({taskId:result.taskId,runtimeStatus:result.status,taskOracle:exists?'pass':'fail',checkpoints:[{id:'file-exists',oracle:exists?'pass':'fail',firstAttempt:exists?'pass':'fail'}],wallMs:performance.now()-started,recoveryObservationComplete:true,modelMode:'scripted'})); trace.push(...collector.events);
}finally{await rm(root,{recursive:true,force:true});}
console.log(JSON.stringify({environment:{platform:process.platform,node:process.version,model:'none-scripted',purpose:'controlled fixtures including deliberate denial; not population success estimate'},...summarizeBatch(runs),trace},null,2));
