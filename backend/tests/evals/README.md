# openEagle Agent 产品回归与诊断测评

> 本目录原有的路由、worker、工具契约用例属于产品回归测试，不计入独立能力分。
> 独立能力校验见 [INDEPENDENT_BENCHMARK.md](INDEPENDENT_BENCHMARK.md)。

这套回归测评覆盖：

1. Agent 设计：main router、worker 类型、solo 路由、事件生命周期和自修复约束。
2. Prompt 设计：直接回答边界、工具错误恢复、精确编辑、停止调用工具和最终答复真实性。
3. Tool loop：工具选择、业务参数、执行结果、错误与重复调用。
4. 执行效率：模型路由、worker 委派、工具调用数量、总耗时和 DeepEval Step Efficiency。
5. 最终交付：任务完成度、事实真实性，以及文件写入类任务的真实产物内容。

## 文件

- `.agent_loop_dataset.json`：10 条人工维护的产品契约用例，包含精确路由、工具和产物要求。
- `agent_loop_harness.py`：在临时工作区运行真实 `AgentRuntime`，并生成 DeepEval trace。
- `test_agent_loop.py`：单轮全链路契约与 traced eval。
- `.dataset.json`：5 条多轮对话 seed 数据集。
- `dataset_augmented.json`：由 DeepEval 生成的 30 条扩充场景。
- `chatbot_callback.py`：多轮模拟使用真实 `AgentRuntime`，保留同一会话的上下文和 worker 状态。
- `test_open_eagle.py`：多轮 ConversationSimulator 测评。
- `metrics.py`：统一评测模型和指标。

## 环境变量

至少设置：

```powershell
$env:DEEPSEEK_API_KEY="<key>"
$env:PYTHONUTF8="1"
```

可选覆盖：

```powershell
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:DEEPSEEK_MODEL="deepseek-chat"
$env:EVAL_MODEL_BASE_URL="<OpenAI-compatible URL>"
$env:EVAL_MODEL_API_KEY="<judge key>"
$env:EVAL_MODEL_NAME="<judge model>"
```

应用模型和 judge 模型可以分开配置，避免同一个模型既答题又给自己打分。

## 运行

只跑零网络成本的结构契约：

```powershell
uv run deepeval test run tests/evals/test_agent_loop.py -m "not agent_eval_live" `
  --identifier "agent-loop-contracts"
```

默认 smoke profile 跑 6 条代表性链路：

```powershell
$env:AGENT_EVAL_PROFILE="smoke"
uv run deepeval test run tests/evals/test_agent_loop.py `
  --identifier "agent-loop-smoke"
```

跑完整 10 条契约：

```powershell
$env:AGENT_EVAL_PROFILE="full"
uv run deepeval test run tests/evals/test_agent_loop.py `
  --identifier "agent-loop-full" `
  --ignore-errors
```

跑多轮会话：

```powershell
$env:CONVERSATION_EVAL_PROFILE="smoke"
uv run deepeval test run tests/evals/test_open_eagle.py `
  --identifier "agent-loop-conversation"
```

设置 `$env:CONVERSATION_EVAL_PROFILE="full"` 时会同时加载 seed 与扩充集，共 35 条场景。

Windows 必须设置 `PYTHONUTF8=1`，否则 DeepEval/Rich 的 Unicode 结果摘要可能被 GBK 控制台拒绝。

## 扩充多轮数据集

现有 `.dataset.json` 是 seed 集。使用 DeepEval CLI 扩充，而不是手写更多相似用例：

```powershell
uv run deepeval generate `
  --method goldens `
  --variation multi-turn `
  --goldens-file tests/evals/.dataset.json `
  --max-goldens-per-golden 6 `
  --scenario-context "桌面端用户使用 openEagle 完成文件、代码、检索、定时任务、记忆和桌面操作" `
  --conversational-task "跨多轮正确恢复上下文，选择合适 worker 和工具，真实完成任务并汇报" `
  --participant-roles "用户与 openEagle main agent" `
  --scenario-format "2-6 轮，包含省略式追问、失败恢复、工具边界和可验证完成条件" `
  --expected-outcome-format "用户目标被真实完成；若失败或需确认，助手如实说明状态" `
  --output-dir tests/evals `
  --file-name .dataset_augmented
```

生成后先人工抽查高风险写入、删除和桌面操作场景，再替换或合并正式数据集。

DeepEval 4.0.7 在 OpenAI-compatible 自定义模型上可能触发
`unsupported operand type(s) for +=: 'int' and 'NoneType'`。遇到该上游问题时使用：

```powershell
uv run python tests/evals/generate_dataset.py
```

这个 fallback 仍使用 DeepEval Synthesizer 和 `save_as`，并兼容 4.0.7
同步 wrapper 重复写入同一批 goldens 的问题。

## 结果解释

这些结果用于定位 openEagle 内部设计回归。因为用例知道预期路由、worker 和工具，
不能拿来证明产品具有通用 Agent 能力，也不能与其他 Agent 横向比较。

- 确定性契约失败：路由、工具、事件、耗时或真实产物不符合产品设计，优先级最高。
- Task Completion / 最终交付真实性失败：结果没有完成目标，或回复与真实执行不一致。
- Tool Correctness / 工具参数正确性失败：工具或参数选择有误。
- Step Efficiency 失败：任务完成了，但 agent handoff、模型轮次或工具调用存在可压缩空间。

未配置 `CONFIDENT_API_KEY` 时结果只保存在本地。配置后可保存历史报告并进行人工标注。
