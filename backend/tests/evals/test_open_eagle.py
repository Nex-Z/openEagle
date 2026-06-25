"""openEagle 多轮对话端到端评估。

使用 DeepEval ConversationSimulator 模拟用户对话，评估 Agent 的
对话质量、角色一致性和工具使用合理性。

运行方式：
    cd backend
    $env:PYTHONUTF8="1"
    uv run deepeval test run tests/evals/test_open_eagle.py

环境变量：
    EVAL_MODEL_BASE_URL / EVAL_MODEL_API_KEY / EVAL_MODEL_NAME
    未设置时回退到 DEEPSEEK_BASE_URL / DEEPSEEK_API_KEY / DEEPSEEK_MODEL
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from deepeval import assert_test
from deepeval.dataset import EvaluationDataset
from deepeval.simulator import ConversationSimulator

# 将 evals 目录加入 Python 路径以导入本地模块
_evals_dir = Path(__file__).resolve().parent
if str(_evals_dir) not in sys.path:
    sys.path.insert(0, str(_evals_dir))

from chatbot_callback import chatbot_callback
from metrics import MULTI_TURN_METRICS, eval_model

# 模拟的最大轮次
MAX_TURNS = 5
PROFILE = os.environ.get("CONVERSATION_EVAL_PROFILE", "smoke").strip().lower()

# 加载数据集
dataset = EvaluationDataset()
dataset.add_goldens_from_json_file(file_path=str(_evals_dir / ".dataset.json"))
if PROFILE == "full":
    dataset.add_goldens_from_json_file(
        file_path=str(_evals_dir / "dataset_augmented.json")
    )

# 创建对话模拟器
simulator = ConversationSimulator(
    model_callback=chatbot_callback,
    simulator_model=eval_model,
    async_mode=True,
    language="Chinese",
)


@pytest.mark.parametrize(
    "test_case",
    simulator.simulate(
        conversational_goldens=dataset.goldens,
        max_user_simulations=MAX_TURNS,
    ),
)
@pytest.mark.agent_eval_live
def test_open_eagle_conversation(test_case):
    """评估 openEagle 的多轮对话质量。"""
    assert_test(test_case=test_case, metrics=MULTI_TURN_METRICS)
