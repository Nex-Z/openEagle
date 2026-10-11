import {join} from 'node:path';
import {BrowserWindow,ipcMain,screen} from 'electron';
import {getActiveWindow,getWindows,mouse,screen as nativeScreen,Region} from '@nut-tree-fork/nut-js';
import {randomUUID,createHash} from 'node:crypto';
import {mkdtemp,writeFile,mkdir,rmdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {CapabilityTicketStore,type DesktopCapability} from './capability-ticket-store';
import {performMouseAction,performKeyboardAction} from './input';
import {GuiAdmission,GuiConsent,activateGuiWindowOnce} from './gui-policy';
import {imageToPngBuffer} from './screenshot';
export interface GuiBinding {
  create():Promise<BrowserWindow>;
  inspect():Promise<Record<string,unknown>>;
  approve(capability:string,args:Record<string,unknown>,stage:number):Promise<boolean>;
  verify(capability:string,args:Record<string,unknown>,stage:number):Promise<boolean>;
  summary():Record<string,unknown>;
}


type Request={kind:string;id:string;requestId:string;capability:DesktopCapability;payload:Record<string,unknown>;confirmationId?:string};
type Approval={capability:string;args:Record<string,unknown>};
let desktopOwner:GuiBroker|undefined;
// Selective CapabilityBroker adaptation: same request/result and ticket store, narrower owned-window driver.
export class GuiBroker {
  private target?:BrowserWindow;
  private targetIdentity:{title:string;nativeHandleHex:string}|undefined;
  private ownsLease=false;
  private readonly leasePath=join(tmpdir(),'openeagle-gui-system-input.lock');
  private admission=new GuiAdmission();
  private tickets=new CapabilityTicketStore();
  private epoch=0;
  private requestId='';
  private controller=new AbortController();
  private queue:Promise<unknown>=Promise.resolve();
  private readonly consent:GuiConsent;
  private readonly approvals:Record<string,unknown>[]=[];
  private activationAttempts=0;
  private nativeFocusCalls=0;
  private nativeFocusReturnType:string|undefined;
  private nativeFocusReturnValue:boolean|null=null;
  private lastForegroundHandle:number|undefined;
  private authorizationSource:string|undefined;
  private stage=0;
  private stopReason='';
  private closing?:Promise<Record<string,unknown>>;
  private pointer?:{x:number;y:number};
  private readonly pointerAudit:Record<string,unknown>[]=[];
  private releasedAt:string|null=null;
  private recordPointer(expected:{x:number;y:number},actual:{x:number;y:number},phase:string){
    this.pointerAudit.push({at:new Date().toISOString(),monotonicMs:performance.now(),stage:this.stage,phase,expected:{...expected},actual:{x:actual.x,y:actual.y},delta:{x:actual.x-expected.x,y:actual.y-expected.y}});
    if(actual.x!==expected.x||actual.y!==expected.y)throw this.stopError('GUI_POINTER_TAKEOVER');
  }
  testReleaseStatus(){if(!this.smoke)throw new Error('GUI_TEST_DISABLED');return {releasedAt:this.releasedAt,stopReason:this.stopReason,evidenceDirectory:this.output};}
  private output='';
  private images:string[]=[];
  private readyResolve?:()=>void;
  private onApprove=(event:Electron.IpcMainEvent)=>{if(!this.controller.signal.aborted&&event.sender===this.target?.webContents&&event.senderFrame===this.target.webContents.mainFrame){this.consent.grant('owned-window-ui-ipc');this.readyResolve?.();}};
  private onCancel=(event:Electron.IpcMainEvent)=>{if(event.sender===this.target?.webContents)this.cancel();};
  constructor(private readonly binding:GuiBinding,private readonly smoke=false){this.consent=new GuiConsent(smoke);}
  async open(requestId:string){
    if(process.platform!=='win32')throw new Error('GUI_WINDOWS_ONLY');
    if(desktopOwner)throw new Error('GUI_DESKTOP_BUSY');desktopOwner=this;
    try{await mkdir(this.leasePath);this.ownsLease=true;}catch{if(desktopOwner===this)desktopOwner=undefined;throw new Error('GUI_DESKTOP_BUSY');}
    this.requestId=requestId;this.epoch=this.tickets.beginAuthorityEpoch();
    this.output=await mkdtemp(join(tmpdir(),'openeagle-gui-evidence-'));this.controller.signal.throwIfAborted();
    const win=this.target=await this.binding.create();
    const title=win.getTitle();this.targetIdentity={title,nativeHandleHex:win.getNativeWindowHandle().toString('hex')};
    win.on('blur',()=>this.cancel('GUI_FOCUS_LOST'));win.on('closed',()=>this.cancel('GUI_WINDOW_CLOSED'));
    ipcMain.on('v2:gui:cancel',this.onCancel);ipcMain.on('v2:gui:approve',this.onApprove);
    win.setTitle(title);this.controller.signal.throwIfAborted();win.show();await new Promise(resolve=>setTimeout(resolve,180));this.controller.signal.throwIfAborted();
    win.on('move',()=>this.cancel('GUI_WINDOW_MOVED'));win.on('resize',()=>this.cancel('GUI_WINDOW_RESIZED'));win.on('minimize',()=>this.cancel('GUI_WINDOW_MINIMIZED'));win.webContents.on('did-start-navigation',()=>this.cancel('GUI_NAVIGATION'));win.webContents.on('render-process-gone',()=>this.cancel('GUI_RENDERER_GONE'));win.webContents.on('zoom-changed',()=>this.cancel('GUI_ZOOM_CHANGED')); // Initial display is user-requested; never refocus after blur.
    await new Promise<void>((resolve,reject)=>{
      const finish=(error?:Error)=>{clearTimeout(timer);this.controller.signal.removeEventListener('abort',abort);this.readyResolve=undefined;if(error){this.cancel(error.message);reject(error);}else resolve();};
      const abort=()=>finish(new Error('GUI_CANCELLED'));
      const timer=setTimeout(()=>finish(new Error('GUI_APPROVAL_TIMEOUT')),this.smoke&&process.env.OPEN_EAGLE_GUI_MANUAL==='1'?300000:30000);
      this.readyResolve=()=>finish();this.controller.signal.addEventListener('abort',abort,{once:true});
      if(this.controller.signal.aborted){abort();return;}if(this.smoke&&process.env.OPEN_EAGLE_GUI_MANUAL!=='1'&&process.env.OPEN_EAGLE_GUI_TEST_AUTHORIZATION==='conversation'){this.consent.grant('conversation-fixture-test');finish();}else if(this.consent.approved)finish();
    });
    this.authorizationSource=this.consent.source;
    if(this.smoke&&this.consent.source==='conversation-fixture-test'&&process.env.OPEN_EAGLE_GUI_INITIAL_ACTIVATION==='1'){
      this.controller.signal.throwIfAborted();this.activationAttempts++;
      const bytes=win.getNativeWindowHandle();const expected=bytes.length===8?Number(bytes.readBigUInt64LE()):bytes.readUInt32LE();
      const native=(await getWindows()).find(w=>(w as unknown as {windowHandle?:number}).windowHandle===expected);
      if(!native)throw this.stopError('GUI_INITIAL_TARGET_UNAVAILABLE');
      this.nativeFocusCalls++;
      await activateGuiWindowOnce(async()=>{const result=await native.focus();this.nativeFocusReturnType=typeof result;this.nativeFocusReturnValue=typeof result==='boolean'?result:null;return result;},async()=>{await new Promise(resolve=>setTimeout(resolve,100));await this.guard();},this.controller.signal);
    }
    await this.guard();return {target:title,evidenceDirectory:this.output};
  }
  private async guard(checkPointer=true,phase='guard'){
    if(this.stopReason)throw new Error(this.stopReason);this.admission.assertActive();this.controller.signal.throwIfAborted();
    const win=this.target;if(!win||win.isDestroyed()||!win.isVisible()||!win.isFocused())throw this.stopError('GUI_FOCUS_LOST');
    const active=await getActiveWindow();this.controller.signal.throwIfAborted();
    // Version-pinned nut-js exposes its native handle on the Window object; fail closed if absent.
    const actual=(active as unknown as {windowHandle?:number}).windowHandle;this.lastForegroundHandle=actual;
    const bytes=win.getNativeWindowHandle();const expected=bytes.length===8?Number(bytes.readBigUInt64LE()):bytes.readUInt32LE();
    if(actual!==expected)throw this.stopError('GUI_NATIVE_HANDLE_MISMATCH');if(!win.isFocused())throw this.stopError('GUI_FOCUS_LOST');
    if(checkPointer&&this.pointer){const position=await mouse.getPosition();this.controller.signal.throwIfAborted();this.recordPointer(this.pointer,position,phase);}
  }
  private stopError(code:string){this.cancel(code);return new Error(code);}
  cancel(reason='GUI_CANCELLED'){if(!this.stopReason)this.stopReason=reason;this.admission.stop();this.consent.revoke();this.controller.abort();this.tickets.revokeAuthorityEpoch(this.epoch);this.readyResolve=undefined;}
  private async inspect(){await this.guard();return {bounds:this.target!.getContentBounds(),state:await this.binding.inspect()};}
  async authorize(approval:Approval){
    if(this.smoke)await (globalThis as any).__guiHooks?.authorize?.(this,approval,this.stage);
    await this.guard();if(!this.consent.approved)throw new Error('GUI_CONFIRMATION_REQUIRED');
    const p=approval.args;await this.validate(p);
    if(!await this.binding.approve(approval.capability,p,this.stage))throw new Error('GUI_PLAN_NOT_APPROVED');
    const cap:DesktopCapability=approval.capability==='desktop_type'?'desktop.keyboard':'desktop.mouse';
    const ticket=randomUUID();if(!this.tickets.grant(ticket,this.requestId,cap,p,this.epoch,2500))throw new Error('GUI_TICKET_REJECTED');this.approvals.push({source:this.consent.source,requestId:this.requestId,target:this.targetIdentity?.title,capability:cap,argsSha256:createHash('sha256').update(JSON.stringify(p)).digest('hex')});return {ticket};
  }
  private async validate(payload:Record<string,unknown>){
    if(payload.target!==this.target?.getTitle())throw new Error('GUI_TARGET_MISMATCH');
    if(typeof payload.observationId!=='string')throw new Error('GUI_OBSERVATION_REQUIRED');
    return this.admission.check(payload.observationId,await this.inspect());
  }
  handle(value:unknown):Promise<unknown>{
    const result=this.queue.then(()=>this.execute(value));this.queue=result.catch(()=>{});return result;
  }
  private async execute(value:unknown){
    const r=value as Request;let inputStarted=false;
    try{
      if(!r||r.kind!=='capability:request'||typeof r.id!=='string'||r.requestId!==this.requestId||!r.payload||typeof r.payload!=='object')throw new Error('GUI_BAD_REQUEST');
      this.admission.admit(r.id);await this.guard();let result:Record<string,unknown>;
      if(r.capability==='screen.capture'){
        if(r.payload.operation==='inspect'){result={fingerprint:await this.validate(r.payload)};}
        else if(r.payload.operation==='oracle'){result={state:await this.binding.inspect()};}
        else if(r.payload.operation==='observe'){
          const inspected=await this.inspect();const observationId=randomUUID();const fingerprint=this.admission.observe(observationId,inspected);
          const bounds=this.target!.getContentBounds();const origin=screen.dipToScreenPoint({x:bounds.x,y:bounds.y});const edge=screen.dipToScreenPoint({x:bounds.x+bounds.width,y:bounds.y+bounds.height});
          const display=screen.getDisplayMatching(bounds);if(display.id!==screen.getPrimaryDisplay().id||bounds.x<display.bounds.x||bounds.y<display.bounds.y||bounds.x+bounds.width>display.bounds.x+display.bounds.width||bounds.y+bounds.height>display.bounds.y+display.bounds.height)throw new Error('GUI_CAPTURE_BOUNDS_REJECTED');
          const image=await nativeScreen.grabRegion(new Region(origin.x,origin.y,edge.x-origin.x,edge.y-origin.y));await this.guard();const png=imageToPngBuffer(image);const name=`window-${this.images.length}.png`;await writeFile(join(this.output,name),png);this.images.push(name);
          result={observationId,fingerprint,state:await this.binding.inspect(),image:name,sha256:createHash('sha256').update(png).digest('hex'),width:image.width,height:image.height,target:this.target!.getTitle()};
        }else throw new Error('GUI_BAD_OPERATION');
      }else{
        if(!['desktop.mouse','desktop.keyboard'].includes(r.capability))throw new Error('GUI_CAPABILITY_DISABLED');
        await this.validate(r.payload);
        if(!this.tickets.consume(r,this.epoch))throw new Error('GUI_TICKET_REQUIRED');
        if(r.capability==='desktop.mouse'){
          const bounds=this.target!.getContentBounds();const {x,y}=r.payload;
          if(typeof x!=='number'||typeof y!=='number'||!Number.isFinite(x)||!Number.isFinite(y)||x<0||y<0||x>=bounds.width||y>=bounds.height)throw new Error('GUI_COORDINATE_REJECTED');
          const point=screen.dipToScreenPoint({x:Math.round(bounds.x+x),y:Math.round(bounds.y+y)});
          this.pointerAudit.push({at:new Date().toISOString(),monotonicMs:performance.now(),phase:'planned-move',contentBoundsDip:bounds,relativeDip:{x,y},physical:point,display:(({id,scaleFactor,bounds})=>({id,scaleFactor,bounds}))(screen.getDisplayMatching(bounds)),displayCount:screen.getAllDisplays().length});
          inputStarted=true;
          await performMouseAction({action:'click',x:point.x,y:point.y},async phase=>{
            if(this.smoke)await (globalThis as any).__guiHooks?.input?.(this,phase,this.stage);
            await this.guard(phase!=='after-move',`mouse:${phase}`);
            if(phase==='after-move'){const actual=await mouse.getPosition();this.controller.signal.throwIfAborted();this.recordPointer(point,actual,'mouse:after-move');this.pointer=point;}
          });
        }else{
          if(typeof r.payload.text!=='string')throw new Error('GUI_TEXT_REQUIRED');
          inputStarted=true;
          await performKeyboardAction({action:'type_text',text:r.payload.text},async phase=>{if(this.smoke)await (globalThis as any).__guiHooks?.input?.(this,phase,this.stage);await this.guard(true,`keyboard:${phase}`);});

        }
        await this.guard();await new Promise(resolve=>setTimeout(resolve,70));await this.guard();this.stage++;
        const verified=await this.binding.verify(r.capability,r.payload,this.stage);if(!verified)this.cancel('GUI_INPUT_UNVERIFIED');
        result={state:await this.binding.inspect(),stage:this.stage,verified};
      }
      return {kind:'capability:result',id:r.id,ok:true,result};
    }catch(error){if(inputStarted)this.cancel('GUI_INPUT_OUTCOME_UNKNOWN');return {kind:'capability:result',id:r?.id??'',ok:false,error:{code:error instanceof Error?error.message:'GUI_FAILED',message:error instanceof Error?error.message:'GUI_FAILED'}};}
  }
  close(metrics:unknown={}):Promise<Record<string,unknown>>{
    if(this.closing)return this.closing;
    this.cancel();
    this.closing=(async()=>{
      try {
        await this.queue;
        const report={runtimeMetrics:metrics,target:this.targetIdentity??null,approvalAudit:this.approvals,activationAttempts:this.activationAttempts,nativeFocusCalls:this.nativeFocusCalls,nativeFocusReturnType:this.nativeFocusReturnType??null,nativeFocusReturnValue:this.nativeFocusReturnValue,lastForegroundHandle:this.lastForegroundHandle??null,authorizationSource:this.authorizationSource??null,stopReason:this.stopReason,pointerAudit:this.pointerAudit,actuation:'nut-js OS input',capture:'nut-js OS region, target content bounds only',state:await this.binding.inspect(),stage:this.stage,images:this.images,...this.binding.summary()};
        if(this.output)await writeFile(join(this.output,'oracle.json'),JSON.stringify(report,null,2));
        return {...report,evidenceDirectory:this.output};
      } finally {
        ipcMain.removeListener('v2:gui:cancel',this.onCancel);ipcMain.removeListener('v2:gui:approve',this.onApprove);
        if(this.target&&!this.target.isDestroyed())this.target.destroy();if(this.ownsLease){await rmdir(this.leasePath);this.ownsLease=false;}if(desktopOwner===this)desktopOwner=undefined;this.releasedAt=new Date().toISOString();
      }
    })();return this.closing;
  }
  testWindow(){if(!this.smoke)throw new Error('GUI_TEST_DISABLED');return this.target;}
}
