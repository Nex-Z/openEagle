from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


SMOKE_IDS = {
    "fact_extraction_exact",
    "code_repair_with_tests",
    "ambiguity_requires_clarification",
    "workspace_boundary_resistance",
    "multi_turn_reference_resolution",
    "no_tool_exact_reasoning",
}


async def evaluate() -> dict[str, Any]:
    profile = os.environ.get("INDEPENDENT_BENCHMARK_PROFILE", "smoke").lower()
    repeats = max(1, int(os.environ.get("INDEPENDENT_BENCHMARK_REPEATS", "1")))
    manifest = json.loads(
        (_evals_dir / "independent_capabilities.json").read_text(encoding="utf-8")
    )
    assert_manifest_is_implementation_independent(manifest)
    cases = (
        manifest
        if profile == "full"
        else [case for case in manifest if case["id"] in SMOKE_IDS]
    )
    results: list[dict[str, Any]] = []
    capability_counts: dict[str, dict[str, int]] = {}

    for case in cases:
        for repeat in range(repeats):
            run = await run_black_box_case(case)
            try:
                failures = verify_case(run, case)
                passed = not failures
                results.append(
                    {
                        "id": case["id"],
                        "capability": case["capability"],
                        "repeat": repeat + 1,
                        "passed": passed,
                        "failures": failures,
                        "tool_calls": len(run.tools_called),
                        "duration_seconds": round(run.duration_seconds, 3),
                        "output": run.final_output,
                    }
                )
                counts = capability_counts.setdefault(
                    case["capability"], {"passed": 0, "total": 0}
                )
                counts["total"] += 1
                counts["passed"] += int(passed)
                print(
                    f"{case['id']} run={repeat + 1} "
                    f"passed={passed} tools={len(run.tools_called)} "
                    f"duration={run.duration_seconds:.2f}s",
                    flush=True,
                )
            finally:
                run.cleanup()

    passed_count = sum(result["passed"] for result in results)
    safety_results = [
        result
        for result in results
        if str(result["capability"]).startswith("safety_")
    ]
    return {
        "benchmark": "open-eagle-independent-capabilities-v1",
        "profile": profile,
        "repeats": repeats,
        "task_pass_rate": passed_count / len(results),
        "passed": passed_count,
        "total": len(results),
        "safety_pass_rate": (
            sum(result["passed"] for result in safety_results) / len(safety_results)
            if safety_results
            else None
        ),
        "capability_pass_rates": {
            capability: counts["passed"] / counts["total"]
            for capability, counts in sorted(capability_counts.items())
        },
        "median_tool_calls": statistics.median(
            result["tool_calls"] for result in results
        ),
        "median_duration_seconds": statistics.median(
            result["duration_seconds"] for result in results
        ),
        "timestamp": datetime.now(UTC).isoformat(),
        "cases": results,
    }


def main() -> int:
    report = asyncio.run(evaluate())
    report_dir = _backend_dir / ".deepeval" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "independent-capabilities-latest.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"Independent benchmark: {report['passed']}/{report['total']} "
        f"({report['task_pass_rate']:.1%}); report={report_path}"
    )
    minimum = os.environ.get("INDEPENDENT_MIN_PASS_RATE")
    if minimum is not None and report["task_pass_rate"] < float(minimum):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
