import {createHash} from 'node:crypto';
// Platform-neutral admission state. The native broker owns focus/window checks and the lease.
export class GuiAdmission {
  private seen=new Set<string>();
  private observations=new Map<string,{at:number;fingerprint:string}>();
  private stopped=false;
  constructor(private readonly now=Date.now,private readonly ttl=2500){}
  stop(){this.stopped=true;this.observations.clear();}
  assertActive(){if(this.stopped)throw new Error('GUI_STOPPED');}
  admit(id:string){this.assertActive();if(!id||id.length>200||this.seen.has(id)||this.seen.size>=128)throw new Error('GUI_REPLAY_OR_LIMIT');this.seen.add(id);}
  observe(id:string,state:unknown){this.assertActive();const fingerprint=this.hash(state);this.observations.set(id,{at:this.now(),fingerprint});if(this.observations.size>16)this.observations.delete(this.observations.keys().next().value!);return fingerprint;}
  check(id:string,state:unknown){this.assertActive();const value=this.observations.get(id);if(!value||this.now()-value.at>this.ttl)throw new Error('GUI_STALE_OBSERVATION');if(value.fingerprint!==this.hash(state))throw new Error('GUI_TARGET_CHANGED');return value.fingerprint;}
  private hash(value:unknown){return createHash('sha256').update(JSON.stringify(value)).digest('hex');}
}

export type GuiApprovalSource='owned-window-ui-ipc'|'conversation-fixture-test';
// 区分窗口内批准通道与宿主测试授权；测试来源不能进入生产批准门。
export class GuiConsent {
  private stopped=false;
  source:GuiApprovalSource|undefined;
  constructor(private readonly allowTest:boolean){}
  grant(source:GuiApprovalSource){if(this.stopped)throw new Error('GUI_STOPPED');if(source==='conversation-fixture-test'&&!this.allowTest)throw new Error('GUI_TEST_APPROVAL_DISABLED');this.source=source;}
  revoke(){this.stopped=true;this.source=undefined;}
  get approved(){return !this.stopped&&this.source!==undefined;}
}
