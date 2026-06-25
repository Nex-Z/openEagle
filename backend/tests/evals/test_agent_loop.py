from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from deepeval import assert_test, log_hyperparameters
from deepeval.dataset import EvaluationDataset, Golden
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

_evals_dir = Path(__file__).resolve().parent
_backend_dir = _evals_dir.parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
if str(_evals_dir) not in sys.path:
    sys.path.insert(0, str(_evals_dir))

from agent_loop_harness import AgentLoopRun, run_agent_golden
from app.langgraph_agent import LangGraphToolAgent
from app.prompts import build_chat_instructions, build_main_router_instructions
from app.subagent_manager import MAX_WORKER_SELF_REPAIR_ATTEMPTS
from metrics import AGENT_LOOP_BASE_METRICS, AGENT_LOOP_TOOL_METRICS


DATASET_PATH = _evals_dir / ".agent_loop_dataset.json"
PROFILE = os.environ.get("AGENT_EVAL_PROFILE", "smoke").strip().lower()

dataset = EvaluationDataset()
dataset.add_goldens_from_json_file(file_path=str(DATASET_PATH))


@log_hyperparameters
def eval_hyperparameters() -> dict[str, str]:
    return {
        "agent_model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "evaluation_model": os.environ.get(
            "EVAL_MODEL_NAME",
            os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        ),
        "agent_architecture": "main-router -> worker -> langgraph-tool-loop",
        "dataset_profile": PROFILE,
        "prompt_contract": "open-eagle-agent-loop-v1",
    }


def _selected_goldens() -> list[Golden]:
    if PROFILE == "full":
        return list(dataset.goldens)
    return [
        golden
        for golden in dataset.goldens
        if bool((golden.additional_metadata or {}).get("smoke"))
    ]


def _golden_id(golden: Golden) -> str:
    return golden.name or golden.input[:40]


def _assert_event_lifecycle(run: AgentLoopRun) -> None:
    event_types = [event["type"] for event in run.events]
    assert "server:status" in event_types, "缺少 AgentRuntime 状态事件"
    assert "server:message" in event_types, "缺少最终消息事件"
    statuses = [
        event["payload"].get("stage")
        for event in run.events
        if event["type"] == "server:status"
    ]
    assert statuses[0] == "thinking"
    assert statuses[-1] == "idle"

    started: set[str] = set()
    finished: set[str] = set()
    for event in run.events:
        if event["type"] != "server:trace":
            continue
        trace = event["payload"].get("trace", {})
        trace_id = str(trace.get("id") or "")
        if trace.get("status") == "started":
            started.add(trace_id)
        if trace.get("status") in {"completed", "error"}:
            finished.add(trace_id)
    assert finished.issubset(started), f"存在没有 started 事件的完成 trace: {finished - started}"


def _assert_artifacts(run: AgentLoopRun, metadata: dict[str, Any]) -> None:
    for artifact in metadata.get("required_artifacts", []):
        path = str(artifact["path"])
        assert path in run.workspace_files, f"预期产物不存在: {path}"
        content = run.workspace_files[path]
        for expected in artifact.get("contains", []):
            assert expected in content, f"{path} 缺少内容: {expected}"
        for forbidden in artifact.get("not_contains", []):
            assert forbidden not in content, f"{path} 仍包含不应存在的内容: {forbidden}"


def _assert_contract(run: AgentLoopRun, golden: Golden) -> None:
    metadata = golden.additional_metadata or {}
    expected_routes = set(metadata.get("expected_routes", []))
    expected_workers = set(metadata.get("expected_worker_kinds", []))
    required_tools = set(metadata.get("required_tools", []))
    required_tool_groups = metadata.get("required_tool_groups", [])
    forbidden_tools = set(metadata.get("forbidden_tools", []))

    assert run.output.strip(), "Agent 最终输出为空"
    assert run.route in expected_routes, (
        f"路由错误: actual={run.route}, expected={sorted(expected_routes)}"
    )
    assert run.worker_kind in expected_workers, (
        f"worker 错误: actual={run.worker_kind}, expected={sorted(expected_workers)}"
    )
    assert required_tools.issubset(set(run.tool_names)), (
        f"缺少工具: required={sorted(required_tools)}, actual={run.tool_names}"
    )
    for group in required_tool_groups:
        assert set(group).intersection(run.tool_names), (
            f"缺少等价工具组中的任一工具: group={group}, actual={run.tool_names}"
        )
    assert not forbidden_tools.intersection(run.tool_names), (
        f"调用了禁用工具: {sorted(forbidden_tools.intersection(run.tool_names))}"
    )
    assert len(run.tools_called) <= int(metadata.get("max_tool_calls", 100)), (
        f"工具调用过多: {len(run.tools_called)} > {metadata.get('max_tool_calls')}"
    )
    assert run.duration_seconds <= float(metadata.get("max_duration_seconds", 300)), (
        f"执行超时: {run.duration_seconds:.2f}s"
    )
    _assert_event_lifecycle(run)
    _assert_artifacts(run, metadata)


def test_agent_design_contracts() -> None:
    router_prompt = "\n".join(build_main_router_instructions())
    assert "answer_directly" in router_prompt
    assert "delegate_existing" in router_prompt
    assert "start_solo" in router_prompt
    assert "worker 选择依据任务所需能力，而非关键词" in router_prompt
    assert "requires_write 与 requires_gui 是意图 hint" in router_prompt

    worker_prompt = "\n".join(
        build_chat_instructions(
            conversation_id="eval-contract",
            selected_tools=[],
            selected_mcp=[],
            selected_skills=[],
        )
    )
    assert "先把错误当成 observation 自己修正并重试" in worker_prompt
    assert "修改后用合适命令验证" in worker_prompt
    assert MAX_WORKER_SELF_REPAIR_ATTEMPTS >= 2


def test_final_answer_prompt_contract() -> None:
    messages = LangGraphToolAgent._messages_for_final_answer(
        [
            SystemMessage(content="system"),
            HumanMessage(content="读取文件"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "read_text_file",
                        "args": {"path": "missing.txt"},
                    }
                ],
            ),
        ]
    )
    system_prompt = str(messages[0].content)
    assert "停止继续调用工具" in system_prompt
    assert "工具结果不足以完成任务" in system_prompt
    assert "不要留空" in system_prompt


@pytest.mark.parametrize("golden", _selected_goldens(), ids=_golden_id)
@pytest.mark.agent_eval_live
def test_agent_loop_end_to_end(golden: Golden) -> None:
    run = asyncio.run(
        run_agent_golden(
            input_text=golden.input,
            expected_output=golden.expected_output,
            expected_tools=golden.expected_tools,
        )
    )
    _assert_contract(run, golden)
    if (golden.additional_metadata or {}).get("metrics_profile") == "contract_only":
        return
    metrics = list(AGENT_LOOP_BASE_METRICS)
    if golden.expected_tools:
        metrics.extend(AGENT_LOOP_TOOL_METRICS)
    assert_test(golden=golden, metrics=metrics)
