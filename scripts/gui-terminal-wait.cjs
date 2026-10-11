// Observe host release independently from the renderer terminal message. Never actuates input.
async function waitForGuiTerminal({read,onRelease,timeoutMs,now=()=>performance.now(),sleep=ms=>new Promise(r=>setTimeout(r,ms))}){
 const started=now();let releasedAt=null;
 while(now()-started<timeoutMs){
  const state=await read();
  if(state.release?.releasedAt&&releasedAt===null){releasedAt=now();onRelease(state.release);}
  if(state.failure)throw new Error('GUI_TRANSPORT_FAILED: '+JSON.stringify(state.failure));
  if(state.terminal)return;
  if(releasedAt!==null&&now()-releasedAt>=3000)throw new Error('GUI_TERMINAL_MISSING_AFTER_RELEASE');
  await sleep(50);
 }
 throw new Error('GUI_TERMINAL_TIMEOUT');
}
module.exports={waitForGuiTerminal};
