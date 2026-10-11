import {contextBridge,ipcRenderer} from 'electron';
contextBridge.exposeInMainWorld('guiFixture',{
  cancel:()=>ipcRenderer.send('v2:gui:cancel'),
  approve:()=>ipcRenderer.send('v2:gui:approve'),
  report:(state:unknown)=>ipcRenderer.send('v2:gui:oracle',state),
});
