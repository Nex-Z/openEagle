"""DeepEval 4.0.7 CLI 自定义模型兼容生成器。

标准入口仍是 `deepeval generate`。当 OpenAI-compatible 模型触发
DeepEval 4.0.7 的 native-model cost=None 缺陷时，使用这个 SDK fallback。
它从 `.dataset.json` seed 扩充约 30 条多轮 golden，并通过 DeepEval
`save_as` 保存到 `dataset_augmented.json`。

运行方式：
    cd backend
    uv run python tests/evals/generate_dataset.py

环境变量：
    EVAL_MODEL_BASE_URL  — 评估模型 API 地址（OpenAI 兼容）
    EVAL_MODEL_API_KEY   — 评估模型 API Key
    EVAL_MODEL_NAME      — 评估模型名称
"""

from __future__ import annotations

import sys
from pathlib import Path

from deepeval.dataset import EvaluationDataset
from deepeval.synthesizer import Synthesizer
from deepeval.synthesizer.config import ConversationalStylingConfig

# 将 evals 目录加入 Python 路径以导入本地模块
_evals_dir = Path(__file__).resolve().parent
if str(_evals_dir) not in sys.path:
    sys.path.insert(0, str(_evals_dir))

from metrics import eval_model


def main():
    seed_file = _evals_dir / ".dataset.json"

    # openEagle 的对话风格配置
    styling = ConversationalStylingConfig(
        scenario_context=(
            "用户在桌面端使用 openEagle AI 助手进行各种任务，包括文件操作、"
            "代码编写、信息查询、定时任务创建、记忆保存等。"
            "openEagle 是一个运行在本地的桌面 Agent，可以操作文件系统、"
            "运行命令、搜索网页、管理定时任务和长期记忆。"
        ),
        conversational_task=(
            "openEagle 助手需要理解用户意图，在需要时使用工具完成任务，"
            "并在完成后给出明确的总结。对于普通聊天直接回答，不调用工具。"
        ),
        participant_roles="用户（桌面端用户）和助手（openEagle AI Agent）",
        scenario_format="2-5 轮的真实用户对话，包含具体的文件路径、命令或任务描述",
        expected_outcome_format="用户任务被正确完成，助手给出清晰的最终结论",
    )

    # 创建 synthesizer
    synthesizer = Synthesizer(
        model=eval_model,
        conversational_styling_config=styling,
        async_mode=True,
        max_concurrent=5,
    )

    dataset = EvaluationDataset()
    dataset.add_goldens_from_json_file(file_path=str(seed_file))

    print(f"正在基于 {len(dataset.goldens)} 条 seed 生成约 30 条多轮 golden...")
    goldens = synthesizer.generate_conversational_goldens_from_goldens(
        goldens=dataset.goldens,
        max_goldens_per_golden=6,
    )
    # DeepEval 4.0.7 的同步 wrapper 会把 async 结果重复 extend 一次。
    synthesizer.synthetic_conversational_goldens = list(goldens)

    output_file = synthesizer.save_as(
        file_type="json",
        directory=str(_evals_dir),
        file_name="dataset_augmented",
    )
    print(f"生成了 {len(goldens)} 个 golden，保存到 {output_file}")


if __name__ == "__main__":
    main()
