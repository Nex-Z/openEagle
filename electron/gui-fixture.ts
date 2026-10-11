import {BrowserWindow,ipcMain} from 'electron';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {GuiBroker,type GuiBinding} from './gui-broker';
import {guiTargetHtml,GUI_TEXT} from './gui-target';
// 仅宿主可创建绑定；固定业务、DOM oracle 和批准范围不进入通用工具/驱动。
export function createGuiFixture(smoke=false):GuiBroker {
  let win:BrowserWindow;
  let state:Record<string,unknown>={value:'',saved:'',clicks:0,inputs:0,trustedClicks:0,trustedInputs:0,focused:''};
  let controls:Record<string,{x:number;y:number}>={};
  const binding:GuiBinding={
    async create(){
      win=new BrowserWindow({title:'OpenEagle GUI '+randomUUID(),width:680,height:620,resizable:false,maximizable:false,show:false,webPreferences:{preload:path.join(__dirname,'gui-target-preload.js'),contextIsolation:true,nodeIntegration:false,sandbox:true}});
      win.on('page-title-updated',e=>e.preventDefault());win.webContents.setWindowOpenHandler(()=>({action:'deny'}));win.webContents.on('will-navigate',e=>e.preventDefault());
      const oracle=(event:Electron.IpcMainEvent,value:Record<string,unknown>)=>{if(event.sender===win.webContents&&event.senderFrame===win.webContents.mainFrame&&value&&typeof value.value==='string'&&value.value.length<=80)state={...value};};
      ipcMain.on('v2:gui:oracle',oracle);win.once('closed',()=>ipcMain.removeListener('v2:gui:oracle',oracle));
      await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(guiTargetHtml));
      controls=await win.webContents.executeJavaScript("Object.fromEntries(['entry','save'].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [id,{x:r.x+r.width/2,y:r.y+r.height/2}];}))");
      return win;
    },
    async inspect(){const {events,...observed}=state;return {...observed,controls};},
    async approve(capability,args,stage){
      if(stage>2)return false;
      if(stage===1)return capability==='desktop_type'&&args.text===GUI_TEXT&&state.focused==='entry';
      const point=controls[stage===0?'entry':'save'];return capability==='desktop_click'&&args.x===point.x&&args.y===point.y;
    },
    async verify(capability,args,stage){
      return capability==='desktop.keyboard'?state.value===args.text&&state.trustedInputs===GUI_TEXT.length:stage===1?state.focused==='entry'&&state.trustedClicks===1:state.saved===GUI_TEXT&&state.trustedClicks===2;
    },
    async textReceipt(prefix){return state.value===prefix&&state.trustedInputs===prefix.length&&state.focused==='entry'&&state.composing===false&&state.selectionStart===prefix.length&&state.selectionEnd===prefix.length;},
    summary(){return {eventAudit:state.events??[],exact:state.saved===GUI_TEXT&&state.trustedClicks===2&&state.trustedInputs===GUI_TEXT.length};},
  };
  return new GuiBroker(binding,smoke);
}
