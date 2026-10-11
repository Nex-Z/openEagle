const {randomUUID}=require('node:crypto');
const readline=require('node:readline');
class FixtureChannel {
 constructor(child,{timeoutMs=3000,onEvent=()=>{},onFailure=()=>{}}={}){
  this.child=child;this.timeoutMs=timeoutMs;this.pending=new Map();this.closed=false;
  this.failure=error=>{if(this.closed)return;this.closed=true;for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(error);}this.pending.clear();onFailure(error);};
  this.lines=readline.createInterface({input:child.stdout});this.lines.on('line',line=>{try{if(Buffer.byteLength(line)>65536)throw Error('FIXTURE_REPLY_TOO_LARGE');const value=JSON.parse(line);if(value.kind==='reply'){const p=this.pending.get(value.id);if(!p)throw Error('FIXTURE_REPLY_ID_MISMATCH');this.pending.delete(value.id);clearTimeout(p.timer);p.resolve(value.state);}else if(['ready','sample','stopped'].includes(value.kind))onEvent(value);else throw Error('FIXTURE_PROTOCOL_REJECTED');}catch(e){this.failure(e);}});
  child.once('error',()=>this.failure(Error('FIXTURE_LAUNCH_FAILED')));child.once('exit',()=>this.failure(Error('FIXTURE_CLOSED')));child.stdin.on('error',()=>this.failure(Error('FIXTURE_PIPE_CLOSED')));
 }
 request(operation){if(this.closed)return Promise.reject(Error('FIXTURE_CLOSED'));const id=randomUUID();return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(id);this.failure(Error('FIXTURE_REPLY_TIMEOUT'));reject(Error('FIXTURE_REPLY_TIMEOUT'));},this.timeoutMs);this.pending.set(id,{resolve,reject,timer});this.child.stdin.write(JSON.stringify({id,operation})+'\n');});}
}
class FocusAudit {
 constructor({now=()=>performance.now(),onViolation=()=>{},limit=8192}={}){this.now=now;this.onViolation=onViolation;this.limit=limit;this.samples=[];this.sequence=0;this.lastElapsed=undefined;this.lastReceived=undefined;this.maxGapMs=0;this.failure=undefined;}
 reject(code){if(!this.failure){this.failure=code;this.onViolation(Error(code));}}
 accept(sample){if(!Number.isSafeInteger(sample.sequence)||sample.sequence!==this.sequence+1||!Number.isFinite(sample.elapsedMs)||sample.elapsedMs<0||(this.lastElapsed!==undefined&&sample.elapsedMs<this.lastElapsed)||!['sentinel','target','other','none'].includes(sample.role)){this.reject('FIXTURE_SAMPLE_INVALID');return;}this.sequence=sample.sequence;if(this.lastElapsed!==undefined){const gap=sample.elapsedMs-this.lastElapsed;this.maxGapMs=Math.max(this.maxGapMs,gap);if(gap>250)this.reject('FIXTURE_SAMPLE_GAP');}this.lastElapsed=sample.elapsedMs;this.lastReceived=this.now();if(this.samples.length>=this.limit){this.reject('FIXTURE_SAMPLE_LIMIT');return;}this.samples.push({sequence:sample.sequence,elapsedMs:sample.elapsedMs,role:sample.role,receivedAtMs:this.lastReceived});if(sample.role!=='sentinel')this.reject('FIXTURE_FOREGROUND_CHANGED');}
 assertFresh(){if(this.failure)throw Error(this.failure);if(this.lastReceived===undefined||this.now()-this.lastReceived>250){this.reject('FIXTURE_SAMPLING_STALE');throw Error(this.failure);}}
 report(){return {status:this.failure?(this.failure==='FIXTURE_FOREGROUND_CHANGED'?'interrupted':'inconclusive'):this.samples.length?'sentinel-at-all-samples':'not-run',failure:this.failure??null,method:'20ms requested polling; actual gaps recorded',provesNoTransientChange:false,count:this.samples.length,maxGapMs:this.maxGapMs,samples:this.samples};}
}
function assertOwnedState(state,identity){if(!state||state.targetHwnd!==identity.targetHwnd||state.sentinelHwnd!==identity.sentinelHwnd||state.targetHwnd===state.sentinelHwnd)throw Error('FIXTURE_IDENTITY_CHANGED');}
async function waitForOwnedUiaStart({read,identity,signal,onWaiting=()=>{},timeoutMs=120000,now=()=>performance.now(),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms))}){
 const start=now();onWaiting();while(now()-start<timeoutMs){signal?.throwIfAborted();const state=await read();assertOwnedState(state,identity);if(state.started===true){if(state.foregroundRole!=='sentinel')throw Error('FIXTURE_SENTINEL_NOT_FOREGROUND');return state;}await sleep(100);}throw Error('FIXTURE_START_TIMEOUT');
}
async function stopOwnedFixture(child,exited,waitForExit,{waitMs=1500}={}){
 const wait=async()=>{let timer;try{await Promise.race([waitForExit,new Promise(resolve=>{timer=setTimeout(resolve,waitMs);})]);}finally{clearTimeout(timer);}};
 if(!exited()){child.stdin.end();await wait();}if(!exited()){child.kill('SIGKILL');await wait();}if(!exited())throw Error('FIXTURE_CLEANUP_INCOMPLETE');
}
module.exports={FixtureChannel,FocusAudit,assertOwnedState,waitForOwnedUiaStart,stopOwnedFixture};