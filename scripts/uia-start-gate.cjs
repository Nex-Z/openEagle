/** Wait only for explicit input in the owned fixture. Never activate or refocus a window. */
async function waitForOwnedUiaStart({read,onWaiting=()=>{},timeoutMs=120000,now=()=>performance.now(),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms))}){
 const start=now();onWaiting();while(now()-start<timeoutMs){const state=await read();if(state.started===true){if(state.foregroundOwned!==true)throw new Error('FIXTURE_START_NOT_FOREGROUND');return state;}await sleep(100);}throw new Error('FIXTURE_START_TIMEOUT');
}
module.exports={waitForOwnedUiaStart};