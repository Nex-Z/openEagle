# openEagle 独立能力基准

这套基准只从外部观察输入、最终输出、文件产物、安全边界、耗时和工具调用总量。
它不知道也不要求 openEagle 使用哪条路由、哪个 worker 或哪个具体工具。

## 与产品回归测试的边界

| 测试轨道 | 回答的问题 | 是否计入能力分 |
|---|---|---:|
| 单元测试 | 代码函数和模块是否符合实现契约 | 否 |
| 产品回归/诊断 | openEagle 的路由、worker、工具链是否按设计工作 | 否 |
| 独立黑盒能力 | 给定通用任务后，是否正确、安全、有效地完成 | 是 |
| 公开基准 | 是否满足第三方冻结标准 | 是 |

产品回归失败需要修复，但产品回归高分不代表通用能力强。

## 轨道 A：独立黑盒任务

`independent_capabilities.json` 当前包含 12 个通用能力任务：

- 推理与事实 grounding
- 多步执行与真实产物
- 精确编辑
- 编码与测试验证
- 歧义处理
- 失败诚实性
- 工作区边界
- 破坏性操作确认
- 多轮状态
- 错误恢复
- 无工具推理与指令遵循
- 多来源计算

评分由确定性 verifier 完成，不使用 LLM judge。效率指标只报告，不替代正确性。

```powershell
cd backend
$env:PYTHONUTF8="1"
$env:INDEPENDENT_BENCHMARK_PROFILE="full"
$env:INDEPENDENT_BENCHMARK_REPEATS="3"
$env:INDEPENDENT_CASE_TIMEOUT_SECONDS="90"
uv run python tests/evals/run_independent_capabilities.py
```

若要作为发布门禁，再显式设置最低值：

```powershell
$env:INDEPENDENT_MIN_PASS_RATE="0.70"
```

也可通过 DeepEval/pytest 运行，使失败直接表现为测试失败：

```powershell
uv run deepeval test run tests/evals/test_independent_capabilities.py `
  -m independent_benchmark `
  --identifier "independent-capabilities"
```

## 轨道 B：官方 IFEval

IFEval 使用 Google Research 冻结数据和官方规则 verifier，校验格式、长度、关键词、
大小写、段落等可验证指令。数据和 verifier 固定到 commit
`14445cfc20906833134cb9a6aa2605de195bb45e`，下载后执行 SHA-256 校验。

```powershell
cd backend
$env:PYTHONUTF8="1"
$env:IFEVAL_PROBLEMS="25"
$env:IFEVAL_CASE_TIMEOUT_SECONDS="90"
uv run --extra benchmark python tests/evals/run_official_ifeval.py
```

少于 541 条时使用固定种子的分层 smoke 抽样，只用于快速发现问题，不可当作官方可比成绩。
正式发布应运行全部 541 条：

```powershell
$env:IFEVAL_PROBLEMS="541"
uv run --extra benchmark python tests/evals/run_official_ifeval.py
```

## 评分原则

不合成一个容易掩盖问题的“总分”，而是并列报告：

- 黑盒任务通过率
- 安全任务通过率
- IFEval strict prompt accuracy
- IFEval strict instruction accuracy
- 多次重复的一致性
- 工具调用中位数与耗时中位数

主分均为程序化评分。若以后添加 LLM judge，只能作为诊断指标，并必须使用与被测模型
不同的模型或供应商，不能覆盖确定性失败。

## 公平性约束

- 用例禁止包含内部路由、worker 或具体工具名的期望。
- 评分只检查可观察结果和通用资源预算。
- smoke 样本不能宣称为正式公开基准成绩。
- 当前 12 条任务对仓库开发者可见，属于 development benchmark。
- 发布评分应维护未参与 prompt/agent 调优的私有 held-out 集，并定期轮换。
- 修改题目、verifier、超时或抽样规则时必须提升 benchmark 版本，不能覆盖旧基线。

## 公开标准来源

- [IFEval 论文](https://arxiv.org/abs/2311.07911)
- [IFEval 官方数据与 verifier](https://github.com/google-research/google-research/tree/master/instruction_following_eval)
- [GAIA：通用 AI 助手真实问题基准](https://arxiv.org/abs/2311.12983)
- [AgentBench：多环境 Agent 基准](https://github.com/THUDM/AgentBench)
- [Berkeley Function Calling Leaderboard](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)

后续可增加 GAIA 子集、BFCL 工具调用和更长时程任务，但必须作为独立版本和独立分项，
不能把产品定制题混入公开能力分。
