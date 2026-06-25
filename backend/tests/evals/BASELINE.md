# Agent Loop 产品回归基线（2026-06-23）

> 这是实现感知的产品诊断结果，已从独立能力评分中排除。
> 独立基线见 [INDEPENDENT_BASELINE.md](INDEPENDENT_BASELINE.md)。

## 已验证

- 结构契约：2/2 通过。
- 相关后端单元测试：87/87 通过。
- `direct_explanation`：Task Completion、Step Efficiency、最终交付真实性均为 1.0。
- `list_workspace`：
  - Task Completion：1.0
  - Tool Correctness：1.0
  - 工具参数正确性：1.0
  - 最终交付真实性：0.9-1.0
  - Step Efficiency：0.5-0.75，低于 0.6 阈值时失败
- `create_verified_file`：
  - 文件真实创建，内容断言通过
  - Task Completion：1.0
  - Tool Correctness：1.0
  - 最终交付真实性：1.0
  - Step Efficiency：0.5
- `read_known_file`：内容 grounding、工具选择和参数均通过；Step Efficiency 为 0.5。
- `missing_file_honesty`：正确判断文件不存在并如实回复；Task Completion 和真实性为 1.0，Step Efficiency 为 0.5。
- `desktop_route`：确定性契约确认路由为 `start_solo`；测试 harness 不执行真实桌面动作。

## 当前产品信号

1. 简单目录列表仍经过 `main router → coding worker → tool`，judge 认为存在额外 handoff。
2. 创建文件前额外调用 `create_directory`，但 `write_text_file` 本身会创建父目录。
3. 用户要求“两句话介绍自己”时，main agent 可能只返回一句，属于 prompt 指令遵循问题。
4. 当前 desktop case 只覆盖 main→solo 调度；真实视觉执行质量由 solo 专项测试负责。

这些失败未通过降低阈值或删除困难用例处理，应作为后续 agent/prompt 优化基线。

## 已处理的测评兼容问题

- DeepEval 4.0.6 CLI 包路径错误：依赖升级到 4.0.7。
- trace 级工具指标需要写入 root trace，而不只写 tool span：已同步。
- 4.0.7 `ArgumentCorrectnessMetric` 模板缺少变量：改用等价的工具参数 GEval。
- worker trace 中的 `agentTaskId`、`workerKind` 属于追踪元数据：构造 ToolCall 时已剥离。
- 4.0.7 CLI 使用 OpenAI-compatible DeepSeek 生成数据时出现 `int += None`：
  保留标准 CLI 命令，并提供 Synthesizer SDK fallback。
- 4.0.7 同步生成 wrapper 会重复 extend：fallback 保存前固定为本次返回的 30 条。
- Windows GBK 无法输出 DeepEval/Rich Unicode 摘要：运行时设置 `PYTHONUTF8=1`。

## 下一轮优化建议

1. 为无需复杂规划的只读单工具任务增加轻量执行路径，减少一次 worker handoff。
2. 在工具 prompt 中明确：`write_text_file` 会自动创建父目录。
3. 加强直接回答对数量、格式和句数约束的遵循。
4. 改动后优先重跑 `direct_identity`、`list_workspace`、`create_verified_file`。
