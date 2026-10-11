export type TextProgress={index:number;char:string;prefix:string};
// Existing Windows driver uses scan codes. Do not silently accept Unicode it cannot represent.
export async function typeGuardedText(text:string,write:(char:string)=>Promise<unknown>,guard:(phase:'before'|'after',progress:TextProgress)=>Promise<void>,receipt?:(progress:TextProgress)=>Promise<void>){
 if(!text||text.length>80||!/^[ -~]+$/.test(text))throw new Error('GUI_NARROW_INPUT_ONLY');
 let prefix='',index=0;
 for(const char of text){prefix+=char;const progress={index:index++,char,prefix};await guard('before',progress);await write(char);await guard('after',progress);if(receipt)await receipt(progress);}
}
export async function waitForTextReceipt(check:()=>Promise<boolean>,guard:()=>Promise<void>,options:{now?:()=>number;sleep?:(ms:number)=>Promise<void>;timeoutMs?:number}={}){
 const now=options.now??(()=>performance.now()),sleep=options.sleep??(ms=>new Promise(r=>setTimeout(r,ms))),start=now();
 while(true){await guard();const matched=await check();await guard();if(matched)return;if(now()-start>=(options.timeoutMs??500))throw new Error('GUI_TEXT_RECEIPT_TIMEOUT');await sleep(10);}
}
