import { lstat, open, realpath } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import type { Capability, ActionOutput } from './contracts.js';

// Only the explicitly selected, host-created sample is exposed. No arbitrary root,
// absolute path, user attachment, shell, browser or desktop capability is accepted.
export async function readonlyWorkspace(root:string):Promise<Capability[]> {
  root=await realpath(root);const rootIdentity=await lstat(root);
  const same=(a:{dev:number;ino:number},b:{dev:number;ino:number})=>a.dev===b.dev&&a.ino===b.ino;
  async function read(){
    const dir=await lstat(root);if(dir.isSymbolicLink()||!same(dir,rootIdentity))throw new Error('WORKSPACE_CHANGED');
    const filename=join(root,'sample.txt'),before=await lstat(filename);
    if(!before.isFile()||before.isSymbolicLink()||before.nlink!==1||before.size>16384)throw new Error('WORKSPACE_FILE_REJECTED');
    const handle=await open(filename,'r');
    try {
      const stat=await handle.stat();if(!same(stat,before)||stat.nlink!==1||stat.size>16384)throw new Error('WORKSPACE_FILE_CHANGED');
      const bytes=Buffer.alloc(16385);let length=0;
      while(length<bytes.length){const result=await handle.read(bytes,length,bytes.length-length,null);if(!result.bytesRead)break;length+=result.bytesRead;}
      if(length>16384||!same(await lstat(root),rootIdentity))throw new Error('WORKSPACE_CHANGED');
      const text=bytes.subarray(0,length).toString('utf8');return {text,bytes:length,hash:createHash('sha256').update(bytes.subarray(0,length)).digest('hex')};
    } finally {await handle.close();}
  }
  return ['workspace_list','workspace_read','workspace_status'].map(name=>({
    name,description:'只读所选临时示例工作区；仅 sample.txt，无用户文件。',availability:{supported:true},
    inputSchema:{type:'object',properties:name==='workspace_read'?{path:{type:'string',enum:['sample.txt']}}:{},...(name==='workspace_read'?{required:['path']}:{}),additionalProperties:false},
    async prepare(args,context,signal){
      signal?.throwIfAborted();if(context.workspace!==root)throw new Error('WORKSPACE_MISMATCH');
      if(!args||typeof args!=='object'||Array.isArray(args)||Object.keys(args).some(k=>name!=='workspace_read'||k!=='path')||(name==='workspace_read'&&args.path!=='sample.txt'))throw new Error('WORKSPACE_ARGUMENT_REJECTED');
      const before=await read();let output:ActionOutput|undefined;
      return {target:'temporary-sample/sample.txt',fingerprint:before.hash,resources:[`workspace:${root}`],effects:['read'],
        async execute(signal){signal.throwIfAborted();const now=await read();if(now.hash!==before.hash)throw new Error('WORKSPACE_CHANGED');
          output={value:name==='workspace_read'?{path:'sample.txt',content:now.text}:name==='workspace_list'?{files:['sample.txt']}:{scope:'temporary-sample',files:1,bytes:now.bytes,readOnly:true},evidence:[{kind:'file-sha256',source:'temporary-sample/sample.txt',value:now.hash}]};return output;
        },
        async verify(value,signal){signal.throwIfAborted();return value===output&&(await read()).hash===before.hash;},
      };
    },
  }));
}
