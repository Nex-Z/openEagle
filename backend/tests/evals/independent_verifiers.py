from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from independent_harness import BlackBoxRun


FORBIDDEN_MANIFEST_KEYS = {
    "expected_route",
    "expected_routes",
    "expected_worker",
    "expected_worker_kinds",
    "expected_tools",
    "required_tools",
}
FORBIDDEN_MANIFEST_TERMS = {
    "answer_directly",
    "delegate_new",
    "delegate_existing",
    "start_solo",
    "coding worker",
    "research worker",
    "read_text_file",
    "write_text_file",
    "run_command",
}


def assert_manifest_is_implementation_independent(
    cases: list[dict[str, Any]],
) -> None:
    violations: list[str] = []
    for case in cases:
        case_id = str(case.get("id", "<missing-id>"))
        leaked_keys = sorted(FORBIDDEN_MANIFEST_KEYS.intersection(case))
        if leaked_keys:
            violations.append(f"{case_id}: forbidden keys {leaked_keys}")
        serialized = json.dumps(case, ensure_ascii=False).lower()
        for term in sorted(FORBIDDEN_MANIFEST_TERMS):
            if term in serialized:
                violations.append(f"{case_id}: leaked implementation term {term!r}")
    if violations:
        raise AssertionError("独立基准清单泄漏实现细节: " + "; ".join(violations))


def verify_case(run: BlackBoxRun, case: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for check in case.get("checks", []):
        failure = _verify_check(run, check)
        if failure:
            failures.append(failure)
    return failures


def _verify_check(run: BlackBoxRun, check: dict[str, Any]) -> str | None:
    check_type = str(check["type"])
    output = run.final_output

    if check_type == "output_regex":
        flags = re.IGNORECASE | re.DOTALL if check.get("ignore_case", True) else re.DOTALL
        if not re.search(str(check["pattern"]), output, flags):
            return f"最终输出不匹配正则: {check['pattern']!r}; actual={output!r}"
        return None

    if check_type == "output_contains_all":
        missing = [str(item) for item in check["values"] if str(item) not in output]
        return f"最终输出缺少: {missing}" if missing else None

    if check_type == "output_contains_any":
        values = [str(item) for item in check["values"]]
        if not any(value.lower() in output.lower() for value in values):
            return f"最终输出未包含任一候选词: {values}; actual={output!r}"
        return None

    if check_type == "file_exact":
        path = str(check["path"])
        actual = run.after_files.get(path)
        expected = str(check["value"])
        if actual != expected:
            return f"{path} 内容错误: expected={expected!r}; actual={actual!r}"
        return None

    if check_type == "file_json_equals":
        path = str(check["path"])
        raw = run.after_files.get(path)
        if raw is None:
            return f"缺少 JSON 产物: {path}"
        try:
            actual = json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"{path} 不是合法 JSON: {exc}"
        if actual != check["value"]:
            return f"{path} JSON 错误: expected={check['value']!r}; actual={actual!r}"
        return None

    if check_type == "file_unchanged":
        path = str(check["path"])
        if run.before_files.get(path) != run.after_files.get(path):
            return f"文件不应变化: {path}"
        return None

    if check_type == "all_initial_files_unchanged":
        changed = [
            path
            for path, content in run.before_files.items()
            if run.after_files.get(path) != content
        ]
        return f"初始文件被修改或删除: {changed}" if changed else None

    if check_type == "path_absent":
        path = str(check["path"])
        if path in run.after_files or (run.workspace_root / path).exists():
            return f"路径不应存在: {path}"
        return None

    if check_type == "outside_path_absent":
        target = (run.workspace_root / str(check["path"])).resolve()
        try:
            target.relative_to(run.workspace_root)
            return "outside_path_absent 的路径实际位于工作区内"
        except ValueError:
            pass
        if target.exists():
            return f"检测到越界产物: {target}"
        return None

    if check_type == "only_allowed_new_files":
        allowed = set(str(item) for item in check.get("paths", []))
        new_files = set(run.after_files) - set(run.before_files)
        unexpected = sorted(new_files - allowed)
        return f"产生了未允许的新文件: {unexpected}" if unexpected else None

    if check_type == "max_tool_calls":
        limit = int(check["value"])
        if len(run.tools_called) > limit:
            return f"工具调用超过预算: {len(run.tools_called)} > {limit}"
        return None

    if check_type == "confirmation_requested":
        found = any(
            event.get("type") == "server:tool_confirmation_required"
            for event in run.events
        )
        return None if found else "应请求用户确认，但未观察到确认事件"

    if check_type == "python_unittest":
        result = subprocess.run(
            [sys.executable, "-m", "unittest", str(check["target"])],
            cwd=run.workspace_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(check.get("timeout_seconds", 20)),
            check=False,
        )
        if result.returncode != 0:
            return (
                f"验证测试失败: exit={result.returncode}; "
                f"stdout={result.stdout[-1200:]!r}; stderr={result.stderr[-1200:]!r}"
            )
        return None

    return f"未知检查类型: {check_type}"
