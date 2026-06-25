from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_evals_dir = Path(__file__).resolve().parent
_backend_dir = _evals_dir.parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))
if str(_evals_dir) not in sys.path:
    sys.path.insert(0, str(_evals_dir))

from official_ifeval import OpenEagleIFEvalModel, PinnedIFEval


def main() -> int:
    problem_count = int(os.environ.get("IFEVAL_PROBLEMS", "25"))
    if problem_count < 1:
        raise ValueError("IFEVAL_PROBLEMS 必须大于 0。")
    cache_dir = _backend_dir / ".deepeval" / "benchmarks"
    benchmark = PinnedIFEval(
        cache_dir=cache_dir,
        n_problems=problem_count,
        verbose_mode=os.environ.get("IFEVAL_VERBOSE", "0") == "1",
        sample_seed=os.environ.get("IFEVAL_SAMPLE_SEED", "open-eagle-ifeval-v1"),
    )
    result = benchmark.evaluate(OpenEagleIFEvalModel())

    report_dir = _backend_dir / ".deepeval" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "benchmark": "IFEval",
        "source": "google-research/instruction_following_eval",
        "data_commit": "14445cfc20906833134cb9a6aa2605de195bb45e",
        "problems": len(result.cases),
        "sampling": (
            "full"
            if len(result.cases) == 541
            else "deterministic-stratified-smoke"
        ),
        "sample_seed": os.environ.get(
            "IFEVAL_SAMPLE_SEED", "open-eagle-ifeval-v1"
        ),
        "selected_keys": [case["key"] for case in result.cases],
        "strict_prompt_accuracy": result.strict_prompt_accuracy,
        "loose_prompt_accuracy": result.loose_prompt_accuracy,
        "strict_instruction_accuracy": result.strict_instruction_accuracy,
        "loose_instruction_accuracy": result.loose_instruction_accuracy,
        "instruction_breakdown": result.instruction_breakdown,
        "cases": result.cases,
        "agent_model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    report_path = report_dir / "ifeval-latest.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"IFEval report: {report_path}")
    print(
        "IFEval scores: "
        f"strict_prompt={result.strict_prompt_accuracy:.4f}, "
        f"loose_prompt={result.loose_prompt_accuracy:.4f}, "
        f"strict_instruction={result.strict_instruction_accuracy:.4f}, "
        f"loose_instruction={result.loose_instruction_accuracy:.4f}"
    )

    minimum = os.environ.get("IFEVAL_MIN_ACCURACY")
    if minimum is not None and result.strict_prompt_accuracy < float(minimum):
        print(
            f"IFEval strict prompt accuracy {result.strict_prompt_accuracy:.4f} "
            f"is below configured gate {float(minimum):.4f}."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
