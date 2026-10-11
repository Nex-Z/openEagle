import {mkdir,writeFile,readFile,unlink,rmdir} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {randomUUID} from 'node:crypto';
/** Shared by the Electron native broker and UIA utility. Never reclaim unknown/stale owners. */
export async function acquireDesktopLease(directory=join(tmpdir(),'openeagle-gui-system-input.lock')){
 const token=randomUUID();try{await mkdir(directory);}catch{throw new Error('GUI_DESKTOP_BUSY');}
 const marker=join(directory,'owner.json');try{await writeFile(marker,JSON.stringify({token,pid:process.pid}),{flag:'wx'});}catch(error){try{await rmdir(directory);}catch{}throw error;}
 let releasing:Promise<void>|undefined;return ()=>releasing??=(async()=>{const owner=JSON.parse(await readFile(marker,'utf8')) as {token?:string};if(owner.token!==token)throw new Error('GUI_LEASE_OWNER_CHANGED');await unlink(marker);await rmdir(directory);})();
}