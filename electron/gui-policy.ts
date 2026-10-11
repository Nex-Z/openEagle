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
