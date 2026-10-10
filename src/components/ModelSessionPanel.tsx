import { useEffect, useRef, useState } from 'react';
import { SecretInput } from './settings/SecretInput';

export function ModelSessionPanel({conversationId,busy,onReady}:{conversationId:string;busy:boolean;onReady:(ready:boolean)=>void}) {
  const alive=useRef(true);useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  const currentConversation=useRef(conversationId);currentConversation.current=conversationId;
  const [provider,setProvider]=useState('openai-like');
  const [endpoint,setEndpoint]=useState('');const [modelId,setModelId]=useState('');
  const [credential,setCredential]=useState('');const [approved,setApproved]=useState(false);
  const [maxCalls,setMaxCalls]=useState(2);const [maxOutputTokens,setMaxOutputTokens]=useState(256);
  const [timeoutMs,setTimeoutMs]=useState(10000);const [open,setOpen]=useState(true);
  const [message,setMessage]=useState('请配置本次会话；密钥仅存内存，不会保存。');
  const [saving,setSaving]=useState(false);
  useEffect(()=>{
    let current=true;setCredential('');setApproved(false);onReady(false);
    void window.electronAPI?.modelSession?.status(conversationId).then(status=>{
      if(!current)return;
      const ready=status.configured===true&&Number(status.calls)<Number(status.maxCalls);
      onReady(ready);setOpen(!ready);
      setMessage(status.configured?`会话调用 ${status.calls}/${status.maxCalls} · 费用 unknown · 仅临时 sample.txt`:'请配置本次会话；密钥仅存内存，不会保存。');
    }).catch(()=>{if(current)setMessage('模型通道正在连接；连接后可配置。');});
    return()=>{current=false;};
  },[conversationId,busy,onReady]);
  async function configure(event:React.FormEvent){
    event.preventDefault();if(busy||saving)return;setSaving(true);
    try {
      await window.electronAPI!.modelSession!.configure({conversationId,credential,config:{provider,api:provider==='openai'?'openai-responses':provider==='anthropic'?'anthropic-messages':'openai-completions',endpoint,modelId,maxCalls,maxOutputTokens,timeoutMs,maxRetries:0,workspace:'temporary-sample',approved}});
      if(!alive.current||currentConversation.current!==conversationId)return;
      onReady(true);setMessage(`已配置：最多 ${maxCalls} 次调用 · 费用 unknown · 密钥不保存`);setOpen(false);
    }catch{if(!alive.current||currentConversation.current!==conversationId)return;onReady(false);setMessage('配置未接受：检查接口、HTTPS 地址、模型和限制；任务运行时不能更换配置。');}
    finally{setCredential('');setApproved(false);setSaving(false);}
  }
  return <details className="pi-session-panel" open={open} onToggle={event=>setOpen(event.currentTarget.open)}>
    <summary>配置本次会话模型 <small>{message}</small></summary>
    <form onSubmit={configure} autoComplete="off">
      <p>只读新建临时目录中的 sample.txt。不会访问日常文件、桌面或网站。真实模式调用可能计费；金额未知，以下限制不是账单硬上限。</p>
      <label>接口<select aria-label="模型接口" value={provider} onChange={e=>setProvider(e.target.value)}><option value="openai-like">OpenAI 兼容 Chat Completions</option><option value="openai">OpenAI Responses</option><option value="anthropic">Anthropic Messages</option></select></label>
      <label>HTTPS API 地址<input aria-label="模型 API 地址" value={endpoint} onChange={e=>setEndpoint(e.target.value)} placeholder="由你的服务商提供" required/></label>
      <label>模型 ID<input aria-label="模型 ID" value={modelId} onChange={e=>setModelId(e.target.value)} required/></label>
      <label>仅本会话的 API Key<SecretInput value={credential} onChange={setCredential} placeholder="在此输入，不要粘贴到聊天中"/></label>
      <div className="pi-budget-fields">
        <label>调用次数<input aria-label="最大调用次数" type="number" min={1} max={4} value={maxCalls} onChange={e=>setMaxCalls(Number(e.target.value))}/></label>
        <label>每次输出 tokens<input aria-label="最大输出 tokens" type="number" min={32} max={2048} value={maxOutputTokens} onChange={e=>setMaxOutputTokens(Number(e.target.value))}/></label>
        <label>每次超时 ms<input aria-label="模型超时" type="number" min={1000} max={60000} value={timeoutMs} onChange={e=>setTimeoutMs(Number(e.target.value))}/></label>
      </div>
      <label className="pi-approval"><input type="checkbox" checked={approved} onChange={e=>setApproved(e.target.checked)}/>我已确认服务地址及本轮调用限制；重试为 0，费用未知。</label>
      <button type="submit" disabled={busy||saving||!approved||!credential}>仅为本会话启用</button>
      <button type="button" disabled={busy||saving} onClick={()=>{void window.electronAPI!.modelSession!.clear(conversationId).then(()=>{if(!alive.current||currentConversation.current!==conversationId)return;onReady(false);setCredential('');setMessage('本会话凭据已清除。');}).catch(()=>setMessage('运行期间不能清除；请先取消任务。'));}}>清除会话凭据</button>
    </form>
  </details>;
}
