from __future__ import annotations

import os

from deepeval.metrics import (
    ConversationCompletenessMetric,
    ConversationalGEval,
    GEval,
    StepEfficiencyMetric,
    TaskCompletionMetric,
    ToolCorrectnessMetric,
    TurnRelevancyMetric,
)
from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import MultiTurnParams, SingleTurnParams


class LocalEvalModel(DeepEvalBaseLLM):
    """使用 DeepSeek API 的评估模型。"""

    def __init__(self):
        from openai import AsyncOpenAI, OpenAI

        self.base_url = os.environ.get(
            "EVAL_MODEL_BASE_URL",
            os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        self.api_key = os.environ.get(
            "EVAL_MODEL_API_KEY",
            os.environ.get("DEEPSEEK_API_KEY", ""),
        )
        self.model_name = os.environ.get(
            "EVAL_MODEL_NAME",
            os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        )
        if not self.api_key:
            raise ValueError("EVAL_MODEL_API_KEY 或 DEEPSEEK_API_KEY 环境变量未设置")
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        self._async_client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)

    def load_model(self):
        return self._client

    def generate(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return response.choices[0].message.content or ""

    async def a_generate(self, prompt: str) -> str:
        response = await self._async_client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return response.choices[0].message.content or ""

    def get_model_name(self) -> str:
        return f"deepseek-{self.model_name}"


# 评估模型实例（所有指标共享）
eval_model = LocalEvalModel()


AGENT_LOOP_BASE_METRICS = [
    TaskCompletionMetric(
        model=eval_model,
        threshold=0.65,
    ),
    StepEfficiencyMetric(
        model=eval_model,
        threshold=0.6,
    ),
    GEval(
        name="最终交付真实性",
        criteria=(
            "判断最终回复是否忠实反映实际执行结果："
            "完成了就给出可核验结果；未完成、被拦截或证据不足时明确说明；"
            "不得把计划、尝试、工具报错或等待确认描述成已经成功。"
        ),
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.EXPECTED_OUTPUT,
        ],
        model=eval_model,
        threshold=0.7,
    ),
]

AGENT_LOOP_TOOL_METRICS = [
    ToolCorrectnessMetric(
        model=eval_model,
        threshold=0.7,
        should_consider_ordering=False,
    ),
    GEval(
        name="工具参数正确性",
        criteria=(
            "根据用户输入判断每次工具调用参数是否正确、最小且安全。"
            "路径必须指向任务要求的目标，查询词和命令必须能直接完成任务；"
            "不得把 agentTaskId、workerKind 等追踪元数据误判为业务参数错误。"
        ),
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.TOOLS_CALLED,
            SingleTurnParams.EXPECTED_OUTPUT,
        ],
        model=eval_model,
        threshold=0.7,
    ),
]

AGENT_LOOP_TRACE_METRICS = [
    *AGENT_LOOP_BASE_METRICS,
    *AGENT_LOOP_TOOL_METRICS,
]


# 多轮对话端到端评估指标（不依赖 tools_called）
MULTI_TURN_METRICS = [
    ConversationCompletenessMetric(
        model=eval_model,
        threshold=0.5,
    ),
    TurnRelevancyMetric(
        model=eval_model,
        threshold=0.5,
    ),
    ConversationalGEval(
        name="回复质量",
        criteria=(
            "评估助手的回复质量："
            "1. 回复内容准确、一致，不自相矛盾"
            "2. 回复直接回答用户问题，不回避"
            "3. 需要操作时给出明确的执行结果或说明"
        ),
        evaluation_params=[
            MultiTurnParams.CONTENT,
            MultiTurnParams.ROLE,
        ],
        model=eval_model,
        threshold=0.5,
    ),
]
