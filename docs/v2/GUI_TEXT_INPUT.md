# 文本填写与按键操作：实现、证据及后续边界

用户已明确：不要长期围绕逐字fixture打补丁；文本填写应优先整段语义写入，按键是最后手段。本轮没有新建UIA/native栈，没有读取或覆盖真实剪贴板，没有自动切输入法，也没有继续前台试验。

## 本轮已落地

新增独立text_fill能力，参数target={kind:browser-element,ref}和text；语义是替换整个字段值，允许Unicode、换行、首尾及连续空格和空字符串。它注册在既有createBrowserCapabilities返回的同一工具集，复用browser_fill的prepare、目标指纹、资源锁、write/external-submit权限、handle.fill及独立字段值验证；记录text-method/browser-semantic-fill证据，不记录文本到新增机制证据中。旧browser_fill保留。跨工具demo已改为text_fill→表单提交→files→CLI。

当前只支持宿主批准的隔离literal-loopback浏览器fixture，不连接日常profile或任意站点；不支持desktop-control目标，不在失败后自动改成鼠键。文本工具可由同一AgentRuntime/Pi注册表使用，但普通Pi产品会话仍只有原来明确注册的临时sample工具，不宣称本轮新增任意网页/桌面路由。真实无头Chrome回归以中文、emoji、重音、连续/首尾空格及换行整段填写，检查字段值和独立HTTP提交值。这个证据是浏览器语义操作，不是OS输入。

原desktop_type仅保留显式前台ASCII扫描码诊断，schema和prepare都提前拒绝多字节及控制字符；其逐字receipt不是产品默认填写方案。自有窗口观测到composition-active时明确GUI_IME_COMPOSITION_ACTIVE停止，而非真值误判成功或等待笼统超时。不发Enter/Esc/Shift提交/取消候选，不改变用户输入法。鼠标精确坐标保护不变。

## 原始现场证据

[第三轮](evidence/windows-gui-manual-2026-10-11-round3/oracle.json)：仅发送O，出现compositionstart/insertCompositionText，无compositionend；最终composing=true，500ms后停止，未发第二字符。87次指针差值0。证明本轮存在未提交的IME组合输入，不证明前两轮全部错字原因。

[第四轮](evidence/windows-gui-manual-2026-10-11-round4/oracle.json)：用户同意手动英文对照，但实际事件仍为上述组合流程，不能仅凭点击认定英文直输。首45次指针读数均(1811,1113)，约358.70ms后读到(1810,1113)，前后采样间隔约14.85ms，立即停止，未保存。

坐标链：控件相对DIP加内容区原点→Math.round得到全局DIP(1207,742)→Electron dipToScreenPoint得到(1811,1113)→nut-js/provider→native绝对坐标SendInput；读回经native GetCursorPos直接传给JS，中间没有第二轮DIP换算。打字期间没有再次setPosition，也没有read→set循环。第一次转换会有整数舍入，但它不能单独解释连续45次一致后才出现的差异；OS坐标上下文、采样或其他移动来源未确定，不指认用户动鼠标，不全局加1或扩大容差。离线回放全部46个样本仍在最后单像素差异停止。

## 已有依赖审计与最小架构

将效果区分为text_set（替换整值，当前text_fill）、text_insert（插入当前选区，尚未通用暴露）、key_action（按键/快捷键，独立前台授权）。宿主只注册实际可用且目标已授权的backend；记录requestedEffect、actualMechanism、目标身份、交互模式和独立结果验证。选择发生在prepare之前，执行失败/unknown后不盲目换通道重试。

|优先级/通道|可复用能力与当前状态|权限及验证边界|
|---|---|---|
|1 API/files/browser语义|browser整段填写本轮已接text_fill；现有API/files按各自语义使用|当前browser仅literal-loopback；保留write/external-submit批准和最终值验证，不伪称OS输入|
|2 桌面辅助功能控件写入|UIA ValuePattern.SetValue是候选；仓库和当前Node依赖没有UIA桥，未实现|只适用支持该pattern的可写控件；不能把TextPattern当通用写接口。目标绑定、enabled/read-only、副作用和后台性逐provider验证；需要新桥/依赖时先报告，不立即安装|
|已有Electron语义（特定宿主）|webContents.insertText是现有依赖接口，尚未接本轮通用工具|只适用持有授权webContents的应用内目标，插入focused element，不适用于任意第三方窗口；需要选区/最终值验证，不能算OS输入，也不能代替通用桌面支持|
|3 原生Unicode文本|现有libnut-win32没有导出此模式；候选是在同一native模块增加独立入口|仍前台系统输入；UTF-16/代理对、UIPI、修饰键、部分送入和取消均需验证。需修改/构建现有native模块，工具链和权限需求先报告；不新造第二套驱动|
|4 显式受控粘贴|Electron clipboard与旧quick-assistant快照代码存在，但不能原样当可靠通道|会接触全局用户数据与剪贴板历史；读取快照需最小化、不得日志持久化。其他应用/用户可并发改变，比较后恢复也非原子。必须明确授权和占用/变化策略，不能覆盖后来内容；本轮不读取、不写入、不测试真实剪贴板|
|5 按键后备|nut-js现有扫描码路径；逐字receipt仅诊断|文本受布局/IME影响，明确foreground/usesSystemInput，独立验证；失败停止，不自动切输入法或再次抢焦点|

旧quick-assistant只快照text/html/rtf/image，随后clear、copy、sleep再恢复：未覆盖所有自定义格式，也没有所有权/并发版本保护。因此没有把这段代码直接复用为text_fill fallback。剪贴板方案不是无风险的便捷替代，不声称一定能完整恢复原状。

## 原生实现和社区经验

已核对本机版本标签及同版[libnut v2.7.5 keypress](https://github.com/ZachJW34/libnut-core/blob/v2.7.5/src/win32/keypress.c)：typeString最终走扫描码，解码后还有char转换，不是Unicode注入。[keycode](https://github.com/ZachJW34/libnut-core/blob/v2.7.5/src/win32/keycode.c)通过VkKeyScan决定键码及修饰键，大写O的Shift序列与本轮事件一致；不能据此证明Shift导致切换输入法。JS逐字符await保持调用顺序，不能证明应用提交文本。

Windows的[KEYBDINPUT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput)明确区分UNICODE/VK_PACKET与SCANCODE。[SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)插入计数不是控件回执，受UIPI与既有键状态约束；不能提权绕过或释放用户按下的键。Unicode入口也不能保证消除已经存在的composition。

[UIA SetValue](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationvaluepattern-setvalue)与[Electron insertText](https://www.electronjs.org/docs/latest/api/web-contents#contentsinserttexttext)为后续目标适配候选，不等于当前已支持。

已只读查阅[nut.js #569](https://github.com/nut-tree/nut.js/issues/569)的Windows中文输出问题，以及[libnut-core #126](https://github.com/nut-tree/libnut-core/issues/126)的历史鼠标归一化问题。这些是版本/触发条件不同的社区线索，不证明本机首O组合或迟发1像素差异的具体根因；本轮读取路径与同版[mouse.c](https://github.com/ZachJW34/libnut-core/blob/v2.7.5/src/win32/mouse.c)核对，不套用旧issue的补偿办法。没有对外发帖、上传日志或声称获得维护者确认。

下一步优先扩展经过授权、可独立验证的语义目标，而不是继续让用户重复逐字fixture。任意桌面UIA/Unicode/受控粘贴仍是明确缺口，需具体选型后实施；本轮不自动新增依赖、权限或前台测试。
