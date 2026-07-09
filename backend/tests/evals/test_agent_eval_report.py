from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from deepeval.test_case import ToolCall

_evals_dir = Path(__file__).resolve().parent
if str(_evals_dir) not in sys.path:
    sys.path.insert(0, str(_evals_dir))

from agent_loop_case_catalog import (
    CORE_CASE_COUNT,
    FULL_CASE_COUNT,
    HOLDOUT_CASE_COUNT,
    TOTAL_CASE_COUNT,
    load_agent_loop_cases,
)
from agent_loop_harness import AgentLoopRun
from run_agent_eval_report import (
    ALLOWED_FAILURE_STAGES,
    DATASET_PATH,
    _json_from_model_text,
    _normalize_judge_result,
    _selected_cases,
    render_markdown,
    validate_dataset,
    verify_rules,
)


def _events(*, tool_error: bool = False) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "server:status", "payload": {"stage": "thinking"}},
        {"type": "server:message", "payload": {"content": "done"}},
        {"type": "server:status", "payload": {"stage": "idle"}},
    ]
    if tool_error:
        events.extend(
            [
                {
                    "type": "server:trace",
                    "payload": {"trace": {"id": "tool-1", "status": "started"}},
                },
                {
                    "type": "server:trace",
                    "payload": {
                        "trace": {
                            "id": "tool-1",
                            "kind": "tool",
                            "name": "read_text_file",
                            "status": "error",
                        }
                    },
                },
            ]
        )
    return events


def _run(
    *,
    output: str = "done",
    route: str = "delegate_new",
    worker_kind: str = "coding",
    tools: list[str] | None = None,
    duration_seconds: float = 1.0,
    workspace_files: dict[str, str] | None = None,
    tool_error: bool = False,
) -> AgentLoopRun:
    return AgentLoopRun(
        input="task",
        output=output,
        route=route,
        worker_kind=worker_kind,
        events=_events(tool_error=tool_error),
        tools_called=[ToolCall(name=name) for name in tools or []],
        duration_seconds=duration_seconds,
        workspace_root=Path("."),
        workspace_files=workspace_files or {},
    )


def _case(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "case",
        "input": "task",
        "expected_output": "done",
        "expected_tools": [],
        "additional_metadata": {
            "layer": "unit",
            "expected_routes": ["delegate_new"],
            "expected_worker_kinds": ["coding"],
            **metadata,
        },
    }


def _stages(failures: list[dict[str, str]]) -> set[str]:
    return {failure["stage"] for failure in failures}


def test_agent_loop_dataset_contract() -> None:
    core_cases = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = load_agent_loop_cases(DATASET_PATH)

    validate_dataset(cases)

    assert len(core_cases) == CORE_CASE_COUNT
    assert len(cases) == TOTAL_CASE_COUNT
    assert len({case["name"] for case in cases}) == TOTAL_CASE_COUNT
    assert "judge" in ALLOWED_FAILURE_STAGES
    assert "constraint_following" in ALLOWED_FAILURE_STAGES
    assert sum("smoke" in case["additional_metadata"]["profiles"] for case in cases) >= 10
    assert sum("core" in case["additional_metadata"]["profiles"] for case in cases) == CORE_CASE_COUNT
    assert sum("full" in case["additional_metadata"]["profiles"] for case in cases) == FULL_CASE_COUNT
    assert sum("holdout" in case["additional_metadata"]["profiles"] for case in cases) == HOLDOUT_CASE_COUNT


def test_report_profiles_select_expected_slices(monkeypatch) -> None:
    cases = load_agent_loop_cases(DATASET_PATH)

    monkeypatch.setenv("AGENT_EVAL_REPORT_PROFILE", "core")
    assert len(_selected_cases(cases)) == CORE_CASE_COUNT

    monkeypatch.setenv("AGENT_EVAL_REPORT_PROFILE", "full")
    assert len(_selected_cases(cases)) == FULL_CASE_COUNT

    monkeypatch.setenv("AGENT_EVAL_REPORT_PROFILE", "holdout")
    assert len(_selected_cases(cases)) == HOLDOUT_CASE_COUNT

    monkeypatch.setenv("AGENT_EVAL_REPORT_PROFILE", "smoke")
    smoke_cases = _selected_cases(cases)
    assert 10 <= len(smoke_cases) < CORE_CASE_COUNT


def test_verify_rules_assigns_diagnostic_stages() -> None:
    assert "routing" in _stages(verify_rules(_run(route="answer_directly"), _case({})))
    assert "tool_selection" in _stages(
        verify_rules(_run(tools=[]), _case({"required_tools": ["read_text_file"]}))
    )
    assert "efficiency" in _stages(
        verify_rules(_run(duration_seconds=20), _case({"max_duration_seconds": 1}))
    )
    assert "artifact" in _stages(
        verify_rules(
            _run(workspace_files={"report.json": "{bad json"}),
            _case(
                {
                    "required_artifacts": [
                        {"path": "report.json", "json_equals": {"ok": True}}
                    ]
                }
            ),
        )
    )
    assert "tool_execution" in _stages(
        verify_rules(_run(tool_error=True), _case({"max_tool_errors": 0}))
    )
    assert "final_answer" in _stages(verify_rules(_run(output=""), _case({})))


def test_verify_rules_rejects_forbidden_artifacts() -> None:
    failures = verify_rules(
        _run(workspace_files={"reports/draft.txt": "old"}),
        _case({"forbidden_artifacts": ["reports/draft.txt"]}),
    )

    assert "artifact" in _stages(failures)


def test_constraint_focus_reclassifies_destructive_failures() -> None:
    failures = verify_rules(
        _run(
            tools=["delete_path"],
            workspace_files={},
        ),
        _case(
            {
                "constraint_focus": True,
                "forbidden_tools": ["delete_path"],
                "required_artifacts": [
                    {"path": "notes/project.txt", "contains": ["Falcon"]}
                ],
            }
        ),
    )

    stages = _stages(failures)
    assert "constraint_following" in stages
    assert "destructive_action_safety" in stages


def test_judge_json_parsing_and_normalization() -> None:
    parsed = _json_from_model_text(
        '```json\n{"verdict":"fail","stage":"routing","confidence":0.8,"reasons":["bad"]}\n```'
    )
    normalized = _normalize_judge_result(parsed)

    assert normalized["verdict"] == "fail"
    assert normalized["stage"] == "routing"
    assert normalized["confidence"] == 0.8
    assert _json_from_model_text("not json")["stage"] == "judge"
    assert _normalize_judge_result({"stage": "surprise"})["stage"] == "judge"


def test_render_markdown_includes_failure_summary_and_judge_reason() -> None:
    markdown = render_markdown(
        {
            "benchmark": "open-eagle-agent-loop-report-v1",
            "profile": "full",
            "timestamp": "2026-07-09T00:00:00+00:00",
            "dataset_total": 20,
            "passed": 1,
            "total": 2,
            "success_rate": 0.5,
            "judge_enabled": True,
            "median_tool_calls": 1,
            "median_duration_seconds": 2,
            "stage_failure_counts": {"routing": 1},
            "failure_category_counts": {"product_failure": 1, "none": 1},
            "layer_pass_rates": {"unit": 0.5},
            "capability_pass_rates": {"routing": 0.5},
            "failure_cases": [
                {
                    "id": "bad_case",
                    "index": 2,
                    "layer": "unit",
                    "stages": ["routing"],
                    "reasons": ["routing: bad route"],
                    "judge_reasons": ["路由选择错误"],
                    "recommended_fix": "调整 router prompt",
                    "failure_category": "product_failure",
                }
            ],
            "cases": [
                {
                    "id": "good_case",
                    "index": 1,
                    "layer": "unit",
                    "passed": True,
                    "route": "delegate_new",
                    "worker_kind": "coding",
                    "tools_called": ["read_text_file"],
                    "duration_seconds": 1,
                    "failure_category": "none",
                    "rule_failures": [],
                    "judge": None,
                },
                {
                    "id": "bad_case",
                    "index": 2,
                    "layer": "unit",
                    "passed": False,
                    "route": "answer_directly",
                    "worker_kind": "general",
                    "tools_called": [],
                    "duration_seconds": 2,
                    "failure_category": "product_failure",
                    "rule_failures": [{"stage": "routing", "message": "bad route"}],
                    "judge": {
                        "verdict": "fail",
                        "stage": "routing",
                        "confidence": 0.9,
                        "reasons": ["路由选择错误"],
                        "recommended_fix": "调整 router prompt",
                    },
                },
            ],
        }
    )

    assert "Success: 1/2 (50.0%)" in markdown
    assert "## Failure Cases" in markdown
    assert "bad_case" in markdown
    assert "路由选择错误" in markdown
