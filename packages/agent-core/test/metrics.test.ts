import test from 'node:test';
import assert from 'node:assert/strict';
import { MetricsCollector, summarizeBatch } from '../src/metrics.js';
test('指标区分runtime完成和oracle成功；失败纳入分母，unknown不算准确',()=>{
  const c=new MetricsCollector();c.collect({taskId:'x',kind:'tool-call',monotonicMs:1,stepId:'s',attemptId:'s'});
  c.collect({taskId:'x',kind:'span-start',monotonicMs:2,spanId:'a',phase:'tool'});c.collect({taskId:'x',kind:'span-end',monotonicMs:7,spanId:'a',phase:'tool'});
  const failed=c.summary({taskId:'x',runtimeStatus:'completed',taskOracle:'fail',checkpoints:[{id:'a',oracle:'unknown',firstAttempt:'unknown'}],wallMs:10,recoveryObservationComplete:true,modelMode:'scripted'});
  assert.equal(failed.cleanSuccess,false);assert.equal(failed.phaseResourceMs.tool,5);assert.equal(failed.checkpointUnknown,1);
  const passed=c.summary({taskId:'y',runtimeStatus:'completed',taskOracle:'pass',checkpoints:[{id:'a',oracle:'pass',firstAttempt:'pass'}],wallMs:20,recoveryObservationComplete:true,modelMode:'scripted'});
  const batch=summarizeBatch([failed,passed]);assert.equal(batch.successRate,.5);assert.equal(batch.oracleCoverage,.5);assert.equal(batch.callsPerSuccessIncludingFailures?.tools,1);assert.equal(batch.costPerSuccess,'unknown');
});

test('最终oracle覆盖与首次尝试覆盖分别计算',()=>{
  const c=new MetricsCollector();const run=c.summary({taskId:'x',runtimeStatus:'completed',taskOracle:'pass',checkpoints:[{id:'x',oracle:'pass',firstAttempt:'unknown'}],wallMs:1,recoveryObservationComplete:false,modelMode:'simulated'});
  const batch=summarizeBatch([run]);assert.equal(batch.oracleCoverage,1);assert.equal(batch.firstAttemptCoverage,0);assert.equal(batch.stepFirstCorrectRate,null);assert.equal(run.cleanSuccess,'unknown');
});
