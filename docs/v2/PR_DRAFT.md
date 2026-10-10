# feat: 整合通用 Agent headless 契约与 Windows 可信工作区验收

文件、命令与桌面操作在既有Python/本地Node迁移中已经存在。本PR新增的是可替换引擎契约、统一准备/审批/执行/验证记录与跨工具验收，不将迁移包装成全新工具。原Electron/React/Python链路保留，UI尚未切换。

## 实际变化

- Pi置于AgentEngine接口后；权限、锁、记录、验证、预算和取消由自有runtime管理。
- 选择性复用旧Node shared.ts路径/目录快照，来源hash与修订记录在REUSE_AUDIT.md。Windows单独opt-in可信工作区factory，Linux行为未改。
- 固定exe/argv取代本切片任意shell；保留普通账号权限和确认，不承诺恶意代码OS沙箱。自有PID树尽力停止、未确认主进程隔离相交工作区，后代状态明确unverified。
- 浏览器仅受控字面loopback fixture；修复HTTP重定向越界，全部3xx拒绝，绑定task ID。runtime cleanup覆盖等待模型/审批时取消，单任务session生命周期明确。
- 新增单调phase事件、model/tool/approval分别计时、调用与usage字段、独立oracle/首次无补救成功率和小样本报告；没有真实费用来源时unknown。

## 验证与边界

- Windows Node24.8/pnpm10.7；真实Chrome154临时profile→文件→CLI→独立服务端/字节/hash断言通过，ScriptedEngine规划。
- 真实Pi包循环+模拟模型transport测试通过；不是付费模型或真实LLM准确率验证。
- 相关完整headless suite与定向复核、Agent/React/Electron类型检查通过，精确次数见VERIFICATION.md。Linux-only测试在Windows跳过，不算通过。
- 指标管线样本分组：3个正常混合任务均通过独立oracle且无补救；1个刻意拒绝写入样本符合安全预期，但业务目标未完成。失败样本的调用开销保留在报告中。合并3/4仅用于检查统计管线，不是产品成功率；小样本不用于性能提升或旧新对比结论。
- 浏览器响应缓冲无硬字节上限，不是通用网站支持或OS网络沙箱；DOM指纹不是敌对页面事务隔离。Windows路径不是所有reparse tag检测，存在并发竞态；taskkill不是Job containment，quarantine仅进程内有效；cleanup等待无硬期限。
- UI尚未接入、Windows UIA/mac Accessibility未实现；既有桌面broker应复用而非重写。最小featureflag/Envelope/确认/取消接线方案在REUSE_AUDIT.md，未复制整个未提交Node迁移。
- 依赖ignore-scripts安装仅支持已验证headless路径，Electron/native二进制安装与打包未验证。没有读取密钥/日常profile、付费调用或操作真实系统设置。

保持Draft，等待父审阅发布；不合并。
