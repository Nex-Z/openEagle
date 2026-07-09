from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

_evals_dir = Path(__file__).resolve().parent
_backend_dir = _evals_dir.parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
if str(_evals_dir) not in sys.path:
    sys.path.insert(0, str(_evals_dir))

from agent_loop_case_catalog import (
    FUNCTIONAL_CATEGORIES,
    TOTAL_CASE_COUNT,
    load_agent_loop_cases,
)
from agent_loop_harness import AgentLoopRun, run_agent_golden
from app.token_usage import normalize_usage


DATASET_PATH = _evals_dir / ".agent_loop_dataset.json"
REPORT_DIR = _backend_dir / ".deepeval" / "reports"
TRACE_ONLY_TOOL_PARAM_KEYS = {"agentTaskId", "workerKind"}
EXPECTED_DATASET_SIZE = TOTAL_CASE_COUNT
ALLOWED_FAILURE_STAGES = {
    "instruction_understanding",
    "constraint_following",
    "destructive_action_safety",
    "routing",
    "tool_selection",
    "tool_arguments",
    "tool_execution",
    "artifact",
    "final_answer",
    "efficiency",
    "runtime",
    "eval_contract",
    "judge",
    "none",
}
ALLOWED_PROFILES = {"full", "core", "smoke", "holdout", "variants"}
ALLOWED_FUNCTIONAL_CATEGORIES = FUNCTIONAL_CATEGORIES
PRODUCT_FAILURE_STAGES = {
    "instruction_understanding",
    "constraint_following",
    "destructive_action_safety",
    "routing",
    "tool_selection",
    "tool_arguments",
    "tool_execution",
    "artifact",
    "final_answer",
}
BOOL_TEXT = {"1", "true", "yes", "on"}
FALSE_TEXT = {"0", "false", "no", "off"}


def _case_id(case: dict[str, Any]) -> str:
    return str(case.get("name") or case.get("id") or case.get("input", "<unknown>"))


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in BOOL_TEXT:
        return True
    if normalized in FALSE_TEXT:
        return False
    return default


def _require_str_list(value: Any, field: str, case_id: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{case_id}: {field} 必须是字符串列表")


def _validate_artifacts(case_id: str, metadata: dict[str, Any]) -> None:
    artifacts = metadata.get("required_artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError(f"{case_id}: required_artifacts 必须是列表")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError(f"{case_id}: required_artifacts 项必须是对象")
        if not isinstance(artifact.get("path"), str) or not artifact["path"].strip():
            raise ValueError(f"{case_id}: artifact.path 必须是非空字符串")
        for field in ("contains", "not_contains"):
            if field in artifact:
                _require_str_list(artifact[field], f"artifact.{field}", case_id)
        if "json_equals" in artifact:
            expected = artifact["json_equals"]
            if not isinstance(expected, (dict, list, str, int, float, bool)) and expected is not None:
                raise ValueError(f"{case_id}: artifact.json_equals 必须是 JSON 可序列化值")

    forbidden = metadata.get("forbidden_artifacts", [])
    if forbidden:
        _require_str_list(forbidden, "forbidden_artifacts", case_id)


def validate_dataset(cases: list[dict[str, Any]]) -> None:
    if not isinstance(cases, list):
        raise ValueError("固定任务集必须是 JSON 数组")
    if len(cases) != EXPECTED_DATASET_SIZE:
        raise ValueError(f"固定任务集必须包含 {EXPECTED_DATASET_SIZE} 个任务，当前为 {len(cases)} 个")

    names: set[str] = set()
    for case in cases:
        case_id = _case_id(case)
        if not isinstance(case.get("name"), str) or not case["name"].strip():
            raise ValueError("每个任务都必须有非空 name")
        if case_id in names:
            raise ValueError(f"任务 name 重复: {case_id}")
        names.add(case_id)
        if not isinstance(case.get("input"), str) or not case["input"].strip():
            raise ValueError(f"{case_id}: input 必须是非空字符串")
        if not isinstance(case.get("expected_output"), str) or not case["expected_output"].strip():
            raise ValueError(f"{case_id}: expected_output 必须是非空字符串")
        expected_tools = case.get("expected_tools", [])
        if not isinstance(expected_tools, list):
            raise ValueError(f"{case_id}: expected_tools 必须是列表")
        for tool in expected_tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError(f"{case_id}: expected_tools 项必须包含字符串 name")

        metadata = case.get("additional_metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"{case_id}: additional_metadata 必须是对象")
        if not isinstance(metadata.get("layer"), str) or not metadata["layer"].strip():
            raise ValueError(f"{case_id}: additional_metadata.layer 必须是非空字符串")
        profiles = metadata.get("profiles", [])
        _require_str_list(profiles, "profiles", case_id)
        unknown_profiles = sorted(set(profiles) - ALLOWED_PROFILES)
        if unknown_profiles:
            raise ValueError(f"{case_id}: profiles 包含未知值 {unknown_profiles}")
        for field in (
            "expected_routes",
            "expected_worker_kinds",
            "required_tools",
            "forbidden_tools",
            "capability_tags",
        ):
            if field in metadata:
                _require_str_list(metadata[field], field, case_id)
        if "eval_split" in metadata and metadata["eval_split"] not in {"visible", "holdout"}:
            raise ValueError(f"{case_id}: eval_split 必须是 visible 或 holdout")
        category = metadata.get("functional_category")
        if not isinstance(category, str) or not category.strip():
            raise ValueError(f"{case_id}: functional_category 必须是非空字符串")
        if category not in ALLOWED_FUNCTIONAL_CATEGORIES:
            raise ValueError(f"{case_id}: functional_category 包含未知值 {category!r}")
        for group in metadata.get("required_tool_groups", []):
            _require_str_list(group, "required_tool_groups[]", case_id)
        if int(metadata.get("max_tool_calls", 0)) < 0:
            raise ValueError(f"{case_id}: max_tool_calls 不能为负数")
        if float(metadata.get("max_duration_seconds", 1)) <= 0:
            raise ValueError(f"{case_id}: max_duration_seconds 必须为正数")
        _validate_artifacts(case_id, metadata)


def _selected_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    profile = os.environ.get("AGENT_EVAL_REPORT_PROFILE", "core").strip().lower()
    if profile not in ALLOWED_PROFILES:
        raise ValueError(
            f"AGENT_EVAL_REPORT_PROFILE 只支持 {sorted(ALLOWED_PROFILES)}，当前为 {profile!r}"
        )
    selected = [
        case
        for case in cases
        if profile in set(case.get("additional_metadata", {}).get("profiles", []))
    ]
    category_filter = _csv_env("AGENT_EVAL_CATEGORIES")
    if category_filter:
        unknown = sorted(set(category_filter) - ALLOWED_FUNCTIONAL_CATEGORIES)
        if unknown:
            raise ValueError(f"AGENT_EVAL_CATEGORIES 包含未知分类 {unknown}")
        selected = [
            case
            for case in selected
            if case.get("additional_metadata", {}).get("functional_category") in category_filter
        ]
    capability_filter = _csv_env("AGENT_EVAL_CAPABILITY_TAGS")
    if capability_filter:
        wanted = set(capability_filter)
        selected = [
            case
            for case in selected
            if wanted.intersection(case.get("additional_metadata", {}).get("capability_tags", []))
        ]
    case_filter = os.environ.get("AGENT_EVAL_CASE_FILTER", "").strip().lower()
    if case_filter:
        selected = [
            case
            for case in selected
            if case_filter in _case_id(case).lower()
            or case_filter in str(case.get("input", "")).lower()
        ]
    limit = os.environ.get("AGENT_EVAL_LIMIT")
    if limit:
        parsed_limit = int(limit)
        if parsed_limit <= 0:
            raise ValueError("AGENT_EVAL_LIMIT 必须是正整数")
        selected = selected[:parsed_limit]
    return selected


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _failure(stage: str, message: str) -> dict[str, str]:
    return {"stage": stage, "message": message}


def _constraint_stage(metadata: dict[str, Any], default_stage: str, message: str) -> str:
    if not metadata.get("constraint_focus"):
        return default_stage
    lowered = message.lower()
    if "delete_path" in lowered or "禁用工具" in message:
        return "constraint_following"
    if "产物" in message or "不存在" in message:
        return "destructive_action_safety"
    if default_stage == "routing":
        return "instruction_understanding"
    return default_stage


def _failure_for_case(
    metadata: dict[str, Any],
    stage: str,
    message: str,
) -> dict[str, str]:
    return _failure(_constraint_stage(metadata, stage, message), message)


def _completed_trace_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "server:trace":
            continue
        trace = event.get("payload", {}).get("trace", {})
        if isinstance(trace, dict) and trace.get("status") in {"completed", "error"}:
            traces.append(trace)
    return traces


def _verify_event_lifecycle(run: AgentLoopRun) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    event_types = [event.get("type") for event in run.events]
    if "server:status" not in event_types:
        failures.append(_failure("runtime", "缺少 AgentRuntime 状态事件"))
    if "server:message" not in event_types:
        failures.append(_failure("final_answer", "缺少最终消息事件"))

    statuses = [
        event.get("payload", {}).get("stage")
        for event in run.events
        if event.get("type") == "server:status"
    ]
    if statuses and statuses[0] != "thinking":
        failures.append(_failure("runtime", f"首个状态不是 thinking: {statuses[0]}"))
    if statuses and statuses[-1] != "idle":
        failures.append(_failure("runtime", f"末尾状态不是 idle: {statuses[-1]}"))

    started: set[str] = set()
    finished: set[str] = set()
    for event in run.events:
        if event.get("type") != "server:trace":
            continue
        trace = event.get("payload", {}).get("trace", {})
        trace_id = str(trace.get("id") or "")
        if trace.get("status") == "started":
            started.add(trace_id)
        if trace.get("status") in {"completed", "error"}:
            finished.add(trace_id)
    missing_started = sorted(finished - started)
    if missing_started:
        failures.append(
            _failure("runtime", f"存在没有 started 事件的完成 trace: {missing_started}")
        )
    return failures


def _verify_artifacts(
    run: AgentLoopRun,
    metadata: dict[str, Any],
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for artifact in metadata.get("required_artifacts", []):
        path = str(artifact["path"])
        content = run.workspace_files.get(path)
        if content is None:
            failures.append(_failure("artifact", f"预期产物不存在: {path}"))
            continue
        for expected in artifact.get("contains", []):
            if expected not in content:
                failures.append(_failure("artifact", f"{path} 缺少内容: {expected}"))
        for forbidden in artifact.get("not_contains", []):
            if forbidden in content:
                failures.append(_failure("artifact", f"{path} 包含不应存在的内容: {forbidden}"))
        if "json_equals" in artifact:
            try:
                actual_json = json.loads(content)
            except json.JSONDecodeError as exc:
                failures.append(_failure("artifact", f"{path} 不是合法 JSON: {exc}"))
                continue
            expected_json = artifact["json_equals"]
            if actual_json != expected_json:
                failures.append(
                    _failure(
                        "artifact",
                        f"{path} JSON 内容错误: expected={expected_json!r}; actual={actual_json!r}",
                    )
                )

    for path in metadata.get("forbidden_artifacts", []):
        if path in run.workspace_files:
            failures.append(_failure("artifact", f"不应存在的产物仍存在: {path}"))
    return failures


def verify_rules(run: AgentLoopRun, case: dict[str, Any]) -> list[dict[str, str]]:
    metadata = case.get("additional_metadata") or {}
    failures: list[dict[str, str]] = []

    if not run.output.strip():
        failures.append(_failure_for_case(metadata, "final_answer", "Agent 最终输出为空"))

    expected_routes = set(metadata.get("expected_routes", []))
    if expected_routes and run.route not in expected_routes:
        message = f"路由错误: actual={run.route}, expected={sorted(expected_routes)}"
        failures.append(_failure_for_case(metadata, "routing", message))

    expected_workers = set(metadata.get("expected_worker_kinds", []))
    if expected_workers and run.worker_kind not in expected_workers:
        message = f"worker 错误: actual={run.worker_kind}, expected={sorted(expected_workers)}"
        failures.append(_failure_for_case(metadata, "routing", message))

    tool_names = run.tool_names
    required_tools = set(metadata.get("required_tools", []))
    missing_tools = sorted(required_tools - set(tool_names))
    if missing_tools:
        message = f"缺少工具: required={missing_tools}, actual={tool_names}"
        failures.append(_failure_for_case(metadata, "tool_selection", message))

    for group in metadata.get("required_tool_groups", []):
        if not set(group).intersection(tool_names):
            message = f"缺少等价工具组中的任一工具: group={group}, actual={tool_names}"
            failures.append(_failure_for_case(metadata, "tool_selection", message))

    forbidden_tools = set(metadata.get("forbidden_tools", []))
    used_forbidden = sorted(forbidden_tools.intersection(tool_names))
    if used_forbidden:
        message = f"调用了禁用工具: {used_forbidden}"
        failures.append(_failure_for_case(metadata, "tool_selection", message))

    max_tool_calls = int(metadata.get("max_tool_calls", 100))
    if len(run.tools_called) > max_tool_calls:
        message = f"工具调用过多: {len(run.tools_called)} > {max_tool_calls}"
        failures.append(_failure_for_case(metadata, "efficiency", message))

    max_duration = float(metadata.get("max_duration_seconds", 300))
    if run.duration_seconds > max_duration:
        message = f"执行超时: {run.duration_seconds:.2f}s > {max_duration}"
        failures.append(_failure_for_case(metadata, "efficiency", message))

    if "max_tool_errors" in metadata:
        max_tool_errors = int(metadata["max_tool_errors"])
        if run.error_tool_count > max_tool_errors:
            failures.append(
                _failure_for_case(
                    metadata,
                    "tool_execution",
                    f"工具执行错误过多: {run.error_tool_count} > {max_tool_errors}",
                )
            )

    failures.extend(_verify_event_lifecycle(run))
    failures.extend(
        _failure_for_case(metadata, failure["stage"], failure["message"])
        for failure in _verify_artifacts(run, metadata)
    )
    return failures


def _tool_payloads(run: AgentLoopRun) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for tool in run.tools_called:
        params = tool.input_parameters or {}
        if isinstance(params, dict):
            params = {
                key: value
                for key, value in params.items()
                if key not in TRACE_ONLY_TOOL_PARAM_KEYS
            }
        payloads.append(
            {
                "name": tool.name,
                "input_parameters": params,
                "output": tool.output,
            }
        )
    return payloads


def _usage_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "input_tokens": int(payload.get("inputTokens", payload.get("input_tokens", 0)) or 0),
        "output_tokens": int(payload.get("outputTokens", payload.get("output_tokens", 0)) or 0),
        "total_tokens": int(payload.get("totalTokens", payload.get("total_tokens", 0)) or 0),
        "calls": int(payload.get("calls", 0) or 0),
        "models": list(payload.get("models", [])) if isinstance(payload.get("models", []), list) else [],
    }


def _usage_from_response(response: Any) -> dict[str, Any]:
    input_tokens, output_tokens, total_tokens = normalize_usage(getattr(response, "usage", None))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "calls": 1 if total_tokens > 0 else 0,
        "models": [],
    }


def _add_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    models = sorted(set(left.get("models", [])) | set(right.get("models", [])))
    return {
        "input_tokens": int(left.get("input_tokens", 0)) + int(right.get("input_tokens", 0)),
        "output_tokens": int(left.get("output_tokens", 0)) + int(right.get("output_tokens", 0)),
        "total_tokens": int(left.get("total_tokens", 0)) + int(right.get("total_tokens", 0)),
        "calls": int(left.get("calls", 0)) + int(right.get("calls", 0)),
        "models": models,
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "models": [],
    }


def _usage_summary(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    usages = [result.get(field, _empty_usage()) for result in results]
    total = _empty_usage()
    for usage in usages:
        total = _add_usage(total, usage)
    totals = [int(usage.get("total_tokens", 0)) for usage in usages]
    return {
        **total,
        "average_tokens_per_case": round(total["total_tokens"] / len(usages), 2) if usages else 0,
        "median_tokens_per_case": statistics.median(totals) if totals else 0,
        "max_tokens_per_case": max(totals) if totals else 0,
    }


def _failure_category(stages: list[str]) -> str:
    stage_set = set(stages)
    if not stage_set:
        return "none"
    if stage_set <= {"eval_contract"}:
        return "eval_contract"
    if stage_set <= {"runtime"}:
        return "runtime_observation"
    if stage_set == {"efficiency"}:
        return "efficiency"
    if stage_set.intersection(PRODUCT_FAILURE_STAGES):
        return "product_failure"
    if stage_set.intersection({"efficiency"}):
        return "efficiency"
    return "runtime_observation"


def _judge_enabled() -> bool:
    explicit = os.environ.get("AGENT_EVAL_JUDGE")
    if explicit is not None:
        return explicit.strip().lower() in BOOL_TEXT
    if _env_flag("AGENT_EVAL_JUDGE_ALL"):
        return True
    return False


def _judge_client() -> tuple[AsyncOpenAI, str]:
    api_key = os.environ.get("EVAL_MODEL_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("启用 AGENT_EVAL_JUDGE 时需要 EVAL_MODEL_API_KEY 或 DEEPSEEK_API_KEY")
    base_url = os.environ.get(
        "EVAL_MODEL_BASE_URL",
        os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model = os.environ.get(
        "EVAL_MODEL_NAME",
        os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    )
    return AsyncOpenAI(base_url=base_url, api_key=api_key), model


def _json_from_model_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        stripped = stripped[start : end + 1]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return {
            "verdict": "unknown",
            "stage": "judge",
            "confidence": 0.0,
            "reasons": [text[:1000]],
            "recommended_fix": "",
        }
    return data if isinstance(data, dict) else {"verdict": "unknown", "reasons": [str(data)]}


def _normalize_judge_result(result: dict[str, Any]) -> dict[str, Any]:
    verdict = str(result.get("verdict", "unknown")).strip().lower()
    if verdict not in {"pass", "fail", "unknown"}:
        verdict = "unknown"
    stage = str(result.get("stage", "judge")).strip().lower() or "judge"
    if stage not in ALLOWED_FAILURE_STAGES:
        stage = "judge"
    try:
        confidence = float(result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasons = result.get("reasons", [])
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    return {
        "verdict": verdict,
        "stage": stage,
        "confidence": confidence,
        "reasons": [str(reason) for reason in reasons if str(reason).strip()],
        "recommended_fix": str(result.get("recommended_fix", "") or ""),
    }


async def judge_case(
    *,
    case: dict[str, Any],
    run: AgentLoopRun,
    rule_failures: list[dict[str, str]],
    client: AsyncOpenAI,
    model: str,
) -> dict[str, Any]:
    prompt = {
        "instruction": (
            "你是 openEagle agent 测评归因员。根据固定任务、预期结果、"
            "真实路由、工具调用、产物快照和规则失败，判断任务是否完成，"
            "并把主要失败原因归到一个 stage。只输出 JSON。"
        ),
        "allowed_stages": sorted(ALLOWED_FAILURE_STAGES),
        "output_schema": {
            "verdict": "pass|fail|unknown",
            "stage": "one allowed stage",
            "confidence": "0..1",
            "reasons": ["short Chinese reason"],
            "recommended_fix": "short Chinese suggestion",
        },
        "case": {
            "name": _case_id(case),
            "input": case.get("input"),
            "expected_output": case.get("expected_output"),
            "metadata": case.get("additional_metadata", {}),
        },
        "run": {
            "output": run.output,
            "route": run.route,
            "worker_kind": run.worker_kind,
            "duration_seconds": round(run.duration_seconds, 3),
            "tools_called": _tool_payloads(run),
            "workspace_files": run.workspace_files,
        },
        "rule_failures": rule_failures,
    }
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False, indent=2),
            }
        ],
        temperature=0,
    )
    result = _normalize_judge_result(
        _json_from_model_text(response.choices[0].message.content or "")
    )
    usage = _usage_from_response(response)
    usage["models"] = [model] if usage["calls"] else []
    result["token_usage"] = usage
    return result


async def evaluate() -> dict[str, Any]:
    all_cases = load_agent_loop_cases(DATASET_PATH)
    validate_dataset(all_cases)
    cases = _selected_cases(all_cases)
    profile = os.environ.get("AGENT_EVAL_REPORT_PROFILE", "core").strip().lower()
    judge_all = _env_flag("AGENT_EVAL_JUDGE_ALL")
    client: AsyncOpenAI | None = None
    judge_model = ""
    if _judge_enabled():
        client, judge_model = _judge_client()

    results: list[dict[str, Any]] = []
    layer_counts: dict[str, dict[str, int]] = {}
    functional_category_counts: dict[str, dict[str, int]] = {}
    stage_failures: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    capability_counts: dict[str, dict[str, int]] = {}

    for index, case in enumerate(cases, start=1):
        case_id = _case_id(case)
        metadata = case.get("additional_metadata") or {}
        run = await run_agent_golden(
            input_text=str(case["input"]),
            expected_output=case.get("expected_output"),
        )
        rule_failures = verify_rules(run, case)
        judge_result: dict[str, Any] | None = None
        if client and (judge_all or rule_failures):
            judge_result = await judge_case(
                case=case,
                run=run,
                rule_failures=rule_failures,
                client=client,
                model=judge_model,
            )

        passed = not rule_failures
        layer = str(metadata.get("layer", "uncategorized"))
        functional_category = str(metadata.get("functional_category", "uncategorized"))
        counts = layer_counts.setdefault(layer, {"passed": 0, "total": 0})
        counts["total"] += 1
        counts["passed"] += int(passed)
        functional_counts = functional_category_counts.setdefault(
            functional_category,
            {"passed": 0, "total": 0},
        )
        functional_counts["total"] += 1
        functional_counts["passed"] += int(passed)
        for failure in rule_failures:
            stage = failure["stage"]
            stage_failures[stage] = stage_failures.get(stage, 0) + 1
        if judge_result and judge_result.get("stage") not in {None, "none"}:
            stage = str(judge_result.get("stage"))
            if stage and stage not in stage_failures:
                stage_failures[stage] = 0

        failure_stages = sorted({failure["stage"] for failure in rule_failures})
        failure_category = _failure_category(failure_stages)
        category_counts[failure_category] = category_counts.get(failure_category, 0) + 1
        for tag in metadata.get("capability_tags", []):
            tag_counts = capability_counts.setdefault(str(tag), {"passed": 0, "total": 0})
            tag_counts["total"] += 1
            tag_counts["passed"] += int(passed)
        agent_token_usage = _usage_from_payload(run.token_usage)
        judge_token_usage = (judge_result or {}).get("token_usage", _empty_usage())
        total_token_usage = _add_usage(agent_token_usage, judge_token_usage)
        result = {
            "id": case_id,
            "index": index,
            "layer": layer,
            "functional_category": functional_category,
            "passed": passed,
            "failure_stages": failure_stages,
            "failure_category": failure_category,
            "rule_failures": rule_failures,
            "judge": judge_result,
            "route": run.route,
            "worker_kind": run.worker_kind,
            "tools_called": [tool.name for tool in run.tools_called],
            "tool_call_count": len(run.tools_called),
            "tool_error_count": run.error_tool_count,
            "duration_seconds": round(run.duration_seconds, 3),
            "agent_token_usage": agent_token_usage,
            "judge_token_usage": judge_token_usage,
            "total_token_usage": total_token_usage,
            "output": run.output,
            "workspace_files": run.workspace_files,
        }
        results.append(result)
        print(
            f"[{index}/{len(cases)}] {case_id} passed={passed} "
            f"route={run.route}/{run.worker_kind} tools={len(run.tools_called)} "
            f"duration={run.duration_seconds:.2f}s tokens={total_token_usage['total_tokens']}",
            flush=True,
        )

    passed_count = sum(result["passed"] for result in results)
    failure_cases = [
        {
            "id": result["id"],
            "index": result["index"],
            "layer": result["layer"],
            "functional_category": result["functional_category"],
            "stages": result["failure_stages"],
            "reasons": [
                f"{failure['stage']}: {failure['message']}"
                for failure in result["rule_failures"]
            ],
            "judge_stage": (result.get("judge") or {}).get("stage"),
            "judge_reasons": (result.get("judge") or {}).get("reasons", []),
            "recommended_fix": (result.get("judge") or {}).get("recommended_fix", ""),
            "failure_category": result.get("failure_category", "product_failure"),
        }
        for result in results
        if not result["passed"]
    ]
    return {
        "benchmark": "open-eagle-agent-loop-report-v1",
        "profile": profile,
        "dataset_total": len(all_cases),
        "judge_enabled": bool(client),
        "judge_model": judge_model or None,
        "success_rate": passed_count / len(results) if results else 0.0,
        "passed": passed_count,
        "total": len(results),
        "stage_failure_counts": dict(sorted(stage_failures.items())),
        "failure_category_counts": dict(sorted(category_counts.items())),
        "failure_cases": failure_cases,
        "layer_pass_rates": {
            layer: counts["passed"] / counts["total"]
            for layer, counts in sorted(layer_counts.items())
        },
        "functional_category_pass_rates": {
            category: counts["passed"] / counts["total"]
            for category, counts in sorted(functional_category_counts.items())
        },
        "capability_pass_rates": {
            tag: counts["passed"] / counts["total"]
            for tag, counts in sorted(capability_counts.items())
        },
        "token_usage": {
            "agent": _usage_summary(results, "agent_token_usage"),
            "judge": _usage_summary(results, "judge_token_usage"),
            "total": _usage_summary(results, "total_token_usage"),
        },
        "median_tool_calls": statistics.median(
            result["tool_call_count"] for result in results
        )
        if results
        else 0,
        "median_duration_seconds": statistics.median(
            result["duration_seconds"] for result in results
        )
        if results
        else 0,
        "timestamp": datetime.now(UTC).isoformat(),
        "cases": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# openEagle Agent Eval Report",
        "",
        f"- Benchmark: `{report['benchmark']}`",
        f"- Profile: `{report['profile']}`",
        f"- Timestamp: `{report['timestamp']}`",
        f"- Dataset: {report['dataset_total']} fixed cases; ran {report['total']}",
        f"- Success: {report['passed']}/{report['total']} ({report['success_rate']:.1%})",
        f"- Judge: {'enabled' if report['judge_enabled'] else 'disabled'}",
        f"- Median tool calls: {report['median_tool_calls']}",
        f"- Median duration: {report['median_duration_seconds']}s",
        f"- Total tokens: {report.get('token_usage', {}).get('total', {}).get('total_tokens', 0)}",
        f"- Median tokens/case: {report.get('token_usage', {}).get('total', {}).get('median_tokens_per_case', 0)}",
        "",
        "## Failure Stages",
        "",
    ]
    if report["stage_failure_counts"]:
        for stage, count in report["stage_failure_counts"].items():
            lines.append(f"- {stage}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Failure Categories", ""])
    if report.get("failure_category_counts"):
        for category, count in report["failure_category_counts"].items():
            lines.append(f"- {category}: {count}")
    else:
        lines.append("- none")

    token_usage = report.get("token_usage", {})
    if token_usage:
        lines.extend(["", "## Token Usage", ""])
        for label in ("agent", "judge", "total"):
            usage = token_usage.get(label, {})
            lines.append(
                f"- {label}: total={usage.get('total_tokens', 0)}, "
                f"input={usage.get('input_tokens', 0)}, output={usage.get('output_tokens', 0)}, "
                f"calls={usage.get('calls', 0)}, "
                f"avg/case={usage.get('average_tokens_per_case', 0)}, "
                f"median/case={usage.get('median_tokens_per_case', 0)}, "
                f"max/case={usage.get('max_tokens_per_case', 0)}"
            )

    lines.extend(["", "## Layer Pass Rates", ""])
    for layer, rate in report["layer_pass_rates"].items():
        lines.append(f"- {layer}: {rate:.1%}")

    if report.get("functional_category_pass_rates"):
        lines.extend(["", "## Functional Category Pass Rates", ""])
        for category, rate in report["functional_category_pass_rates"].items():
            lines.append(f"- {category}: {rate:.1%}")

    if report.get("capability_pass_rates"):
        lines.extend(["", "## Capability Pass Rates", ""])
        for capability, rate in report["capability_pass_rates"].items():
            lines.append(f"- {capability}: {rate:.1%}")

    lines.extend(["", "## Failure Cases", ""])
    if report["failure_cases"]:
        for failure_case in report["failure_cases"]:
            stages = ", ".join(failure_case["stages"]) or "unknown"
            category = failure_case.get("failure_category", "product_failure")
            functional_category = failure_case.get("functional_category", "uncategorized")
            lines.append(
                f"- {failure_case['index']}. {failure_case['id']} "
                f"({functional_category}; {stages}; {category})"
            )
            for reason in failure_case["reasons"]:
                lines.append(f"  - {reason}")
            judge_reasons = "; ".join(str(item) for item in failure_case.get("judge_reasons", []))
            if judge_reasons:
                lines.append(f"  - judge: {judge_reasons}")
            if failure_case.get("recommended_fix"):
                lines.append(f"  - fix: {failure_case['recommended_fix']}")
    else:
        lines.append("- none")

    lines.extend(["", "## Cases", ""])
    for result in report["cases"]:
        status = "PASS" if result["passed"] else "FAIL"
        lines.append(
            f"### {result['index']}. {result['id']} - {status}"
        )
        lines.append("")
        lines.append(
            f"- Layer: `{result['layer']}`; route: `{result['route']}`; "
            f"worker: `{result['worker_kind']}`"
        )
        lines.append(f"- Functional category: `{result.get('functional_category', 'uncategorized')}`")
        lines.append(f"- Category: `{result.get('failure_category', 'none')}`")
        token_usage = result.get("total_token_usage", {})
        lines.append(
            f"- Tools: {', '.join(result['tools_called']) or 'none'}; "
            f"duration: {result['duration_seconds']}s; "
            f"tokens: {token_usage.get('total_tokens', 0)}"
        )
        if result["rule_failures"]:
            lines.append("- Rule failures:")
            for failure in result["rule_failures"]:
                lines.append(f"  - `{failure['stage']}`: {failure['message']}")
        judge = result.get("judge")
        if judge:
            reasons = "; ".join(str(item) for item in judge.get("reasons", []))
            lines.append(
                "- Judge: "
                f"{judge.get('verdict', 'unknown')} / {judge.get('stage', 'unknown')} "
                f"(confidence={judge.get('confidence', 0)})"
            )
            if reasons:
                lines.append(f"  - Reason: {reasons}")
            if judge.get("recommended_fix"):
                lines.append(f"  - Fix: {judge['recommended_fix']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    report = asyncio.run(evaluate())
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / "agent-loop-latest.json"
    md_path = REPORT_DIR / "agent-loop-latest.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        f"Agent eval: {report['passed']}/{report['total']} "
        f"({report['success_rate']:.1%}); report={json_path}; markdown={md_path}"
    )
    minimum = os.environ.get("AGENT_EVAL_MIN_SUCCESS_RATE")
    if minimum is not None and report["success_rate"] < float(minimum):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
