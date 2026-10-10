# 首个切片验证与 Windows 移交

日期：2026-10-10。基础提交：`a0b3cdc98a9e3ff1e794435f1daaeeb699f85f7e`。
云端 Linux，Node v24.19.0。未访问用户 Windows 工作区，未推送，未创建 PR。

## 通过

- `./node_modules/.bin/tsc --noEmit`：原 React 类型检查
- `./node_modules/.bin/tsc -p tsconfig.electron.json --noEmit`：原 Electron 类型检查
- `./node_modules/.bin/tsc && ./node_modules/.bin/vite build`：原前端生产构建通过（2065 modules）
- `UV_CACHE_DIR=/tmp/openeagle-uv-cache uv run --no-project python -m compileall -q backend/app`：Python 语法检查通过，不代表 Python 依赖/行为回归通过
- `./node_modules/.bin/tsc -p packages/agent-core/tsconfig.json`：新包编译通过
- `node --test packages/agent-core/dist/test/runtime.test.js packages/agent-core/dist/test/files-cli.test.js packages/agent-core/dist/test/pi.test.js`：31 passed、0 failed、0 skipped
  - 10 个运行时用例：默认拒绝各副作用、验证失败、批准参数/版本绑定、共享锁、取消/超时、动作预算、不支持能力、展示异常隔离、防记录篡改、准备取消
  - 5 个文件/CLI 用例：真实临时文件与 SHA-256、越界/symlink、命令白名单与最小环境、非零退出/超限/超时/取消、普通后代清理
  - 16 个真实 Pi 1.1.0 循环 + 离线假传输用例：宿主工具结果、失败语义、取消、轮数预算、schema/未知工具、模型自报成功不能制造证据
- 最终复跑曾暴露共享锁测试依赖定时器的偶发失败；改为显式开始/释放屏障后再次运行，31 项全部通过（修复测试同步，不放宽断言）
- 独立只读审查复跑上述 31 项及新包类型检查通过；修复记录可修改、表单字段未绑定、prepare 未取消、contenteditable 验证问题后再次审查通过
- pnpm 10.7.0 `install --lockfile-only --frozen-lockfile --ignore-scripts --offline` 通过，确认锁文件与当前 workspace 一致
- `git diff --check` 通过

安装说明：首次实际依赖安装使用环境提供的 pnpm 11.25.0，`--ignore-scripts`，仅官方 registry。随后用项目约定 pnpm 10.7.0 校验 frozen lockfile。未更新原根依赖的声明范围；新包依赖精确固定，锁文件包含新增传递依赖及 peer 快照调整。曾遇默认 npm/pnpm cache 路径不可写，改用 /tmp 缓存；几次在线 pnpm10 lock-only 尝试被工具自动审批取消，没有把取消报告成成功，最终以现有锁文件做离线 frozen 校验。

## 阻塞 / 尚未通过

真实浏览器执行：云端受限运行时无法启动 Chromium。原五个真实浏览器用例全部在启动阶段失败（0 skipped），未执行页面操作；获批升级权限重试仍相同失败。没有改成 mock 或忽略失败。

尝试命令：在 `packages/agent-core` 运行 `node --test dist/test/browser.test.js`，默认和获批 escalated 两种执行模式。可执行程序 `/usr/bin/chromium`，Playwright 默认已含 `--no-sandbox`，临时独立 user-data-dir、`--remote-debugging-pipe`、headless。

核心错误：

```text
FATAL:chrome/browser/process_singleton_posix.cc:297
Check failed: . socket() failed: Operation not permitted (1)
```

伴随信息：只读 HOME 下 crash report settings.dat 无法写入、ptrace 不允许；进程 SIGABRT。失败后浏览器与本地 fixture server 已清理。没有连接用户浏览器、登录资料或外部站点。

新增三项浏览器回归（隐藏字段/submitter 改动、页面无限 JS 时取消 prepare、contenteditable 验证）已编译，但按用户最新迁移指示不再重跑云浏览器。最终浏览器 suite 是 8 项，均需在允许 Chromium 的环境实际执行。跨 browser/file/CLI 演示与独立服务端/字节/摘要断言已实现，但完整闭环未通过云端验收；不可宣称成功。

未运行：真实付费/远程模型、完整 Python unittest suite、Electron GUI/安装包、Windows/macOS 原生操作、远端 CI。既有安全问题没有并入此次切片。

## Windows 接手必读

1. 在独立目录导入本地 bundle/patch，勿覆盖用户已有未提交 Node 迁移
2. 先读 `docs/v2/README.md` 和本文件，确认 main/master 的基础差异再整合
3. 可先跑类型检查、Pi/运行时测试和 Chromium browser suite；需要安装 Node >=22.19、pnpm10.7、Playwright Chromium
4. 当前 files/CLI 实现明确 Linux-only，Windows 状态 unavailable；文件/CLI Linux tests 和完整 demo 在非 Linux 被明确 skip，不能把 skip 当 Windows 实测通过
5. Windows 下完成安全文件操作和 Job Object/等效进程监督适配后，再运行完整 browser→file→CLI demo 与独立 oracle
6. 后续才接 Electron/CJS 与 ESM entry、可信批准 UI、取消映射、身份认证传输和 Python feature flag 回退。现 UI 仍完全走旧 Python
7. 本地提交作者/提交者必须是 `ZZM-user <z2657272578@gmail.com>`。没有替用户改 git 全局配置，没有推送
