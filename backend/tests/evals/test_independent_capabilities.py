from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from deepeval import assert_test
from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase

_evals_dir = Path(__file__).resolve().parent
_backend_dir = _evals_dir.parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
if str(_evals_dir) not in sys.path:
    sys.path.insert(0, str(_evals_dir))

from independent_harness import run_black_box_case
from independent_verifiers import (
    assert_manifest_is_implementation_independent,
    verify_case,
)


MANIFEST_PATH = _evals_dir / "independent_capabilities.json"
PROFILE = os.environ.get("INDEPENDENT_BENCHMARK_PROFILE", "smoke").strip().lower()
REPEATS = max(1, int(os.environ.get("INDEPENDENT_BENCHMARK_REPEATS", "1")))
SMOKE_IDS = {
    "fact_extraction_exact",
    "code_repair_with_tests",
    "ambiguity_requires_clarification",
    "workspace_boundary_resistance",
    "multi_turn_reference_resolution",
    "no_tool_exact_reasoning",
}


def _load_cases() -> list[dict[str, Any]]:
    cases = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if PROFILE == "full":
        return cases
    return [case for case in cases if case["id"] in SMOKE_IDS]


CASES = _load_cases()
PARAMS = [
    pytest.param(case, repeat, id=f"{case['id']}-run-{repeat + 1}")
    for case in CASES
    for repeat in range(REPEATS)
]


class DeterministicCapabilityMetric(BaseMetric):
    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        self.threshold = 1.0
        self.async_mode = False
        self.include_reason = True
        self.evaluation_model = "deterministic-black-box-verifier"

    def measure(self, test_case: LLMTestCase, *args: Any, **kwargs: Any) -> float:
        _ = (test_case, args, kwargs)
        self.score = 0.0 if self.failures else 1.0
        self.success = not self.failures
        self.reason = "; ".join(self.failures) if self.failures else "全部确定性检查通过"
        return self.score

    async def a_measure(
        self,
        test_case: LLMTestCase,
        *args: Any,
        **kwargs: Any,
    ) -> float:
        return self.measure(test_case, *args, **kwargs)

    def is_successful(self) -> bool:
        return bool(self.success)

    @property
    def __name__(self) -> str:
        return "Independent Capability Outcome"


def test_manifest_is_implementation_independent() -> None:
    cases = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert_manifest_is_implementation_independent(cases)


@pytest.mark.independent_benchmark
@pytest.mark.parametrize(("case", "repeat"), PARAMS)
def test_independent_capability(case: dict[str, Any], repeat: int) -> None:
    _ = repeat
    run = asyncio.run(run_black_box_case(case))
    try:
        failures = verify_case(run, case)
        assert_test(
            test_case=LLMTestCase(
                input="\n\n".join(run.prompts),
                actual_output=run.final_output,
                tools_called=run.tools_called or None,
                metadata={
                    "caseId": case["id"],
                    "capability": case["capability"],
                    "durationSeconds": round(run.duration_seconds, 3),
                },
            ),
            metrics=[DeterministicCapabilityMetric(failures)],
            run_async=False,
        )
    finally:
        run.cleanup()
