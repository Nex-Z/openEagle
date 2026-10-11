// Owned-window fixture only. DOM is the independent result oracle, never the input actuator.
export const GUI_TEXT='OpenEagle native input';
export const guiTargetHtml=`<!doctype html><meta charset="utf-8"><title>OpenEagle GUI fixture</title>
<style>body{font:18px system-ui;margin:40px;background:#f5f7fa;color:#182133}button,input{font:inherit;padding:14px;border:1px solid #94a3b8;border-radius:6px}input{display:block;width:410px;margin:28px 0}button{background:#dbeafe;cursor:pointer}#result{margin-top:24px;padding:18px;background:white}small{display:block;margin:18px 0;color:#475569}</style>
<h2>真实 GUI · 前台占用验收窗口</h2><p>脚本规划；真实鼠标与键盘；不调用模型。</p>
<small>允许一次：点击输入框 → 输入 OpenEagle native input → 点击保存。会占用全局鼠标和键盘。失焦、指针被移走或取消会停止，不自动恢复；无法识别所有人工输入。</small>
<button id="stop" onclick="window.guiFixture.cancel()">紧急停止 / Esc</button><button id="approve">批准本次固定流程</button><input id="entry" aria-label="验收文本" autocomplete="off" spellcheck="false" placeholder="等待真实键盘输入"><button id="save">保存样本</button><div id="result">尚未保存</div>
<script>
const state={value:'',saved:'',clicks:0,inputs:0,trustedClicks:0,trustedInputs:0,approved:false};
const entry=document.querySelector('#entry'),save=document.querySelector('#save');
function report(){window.guiFixture.report({...state,focused:document.activeElement.id});}
document.querySelector('#approve').onclick=()=>{state.approved=true;window.guiFixture.approve();report();};
entry.addEventListener('input',e=>{state.value=entry.value;state.inputs++;if(e.isTrusted)state.trustedInputs++;report();});
entry.addEventListener('click',e=>{state.clicks++;if(e.isTrusted)state.trustedClicks++;report();});
save.addEventListener('click',e=>{state.clicks++;if(e.isTrusted)state.trustedClicks++;state.saved=entry.value;document.querySelector('#result').textContent='已保存：'+state.saved;report();});
window.addEventListener('keydown',e=>{if(e.key==='Escape')window.guiFixture.cancel();});window.addEventListener('focus',report);entry.addEventListener('focus',report);report();
</script>`;
