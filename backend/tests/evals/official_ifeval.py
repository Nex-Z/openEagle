from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from independent_harness import run_black_box_case


IFEVAL_COMMIT = "14445cfc20906833134cb9a6aa2605de195bb45e"
IFEVAL_SHA256 = "67ffeee0fcb87c317c5b08a2de85557b4a7e96ada6178aa645b4954fe4b53d49"
IFEVAL_BASE_URL = (
    "https://raw.githubusercontent.com/google-research/google-research/"
    f"{IFEVAL_COMMIT}/instruction_following_eval"
)
IFEVAL_URL = f"{IFEVAL_BASE_URL}/data/input_data.jsonl"
OFFICIAL_VERIFIER_FILES = {
    "instructions.py": "130f9c50e15ae44820c9ef5b4aa2aa948c4c0a17f4c44c2932b9271add22c6d7",
    "instructions_registry.py": "ec92d72c264f6d906978613085db262356174300370a3fffe6fefd5969ce9cfc",
    "instructions_util.py": "a73797261eee5bf447e279d82a2b700b1bdd3cb1193412dbab1270a85832bc6b",
}


@dataclass
class IFEvalResult:
    strict_prompt_accuracy: float
    loose_prompt_accuracy: float
    strict_instruction_accuracy: float
    loose_instruction_accuracy: float
    instruction_breakdown: dict[str, dict[str, float]]
    cases: list[dict[str, Any]]

    @property
    def overall_accuracy(self) -> float:
        return self.strict_prompt_accuracy


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download_verified(url: str, expected_sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if _sha256(data) != expected_sha256:
        raise RuntimeError(f"官方 IFEval 资源校验失败: {url}")
    return data


def ensure_ifeval_data(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"ifeval-input-{IFEVAL_COMMIT[:12]}.jsonl"
    if path.exists() and _sha256(path.read_bytes()) == IFEVAL_SHA256:
        return path
    path.write_bytes(_download_verified(IFEVAL_URL, IFEVAL_SHA256))
    return path


def _ensure_official_verifier(cache_dir: Path) -> Any:
    package_dir = cache_dir / "instruction_following_eval"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").touch(exist_ok=True)
    for filename, expected_hash in OFFICIAL_VERIFIER_FILES.items():
        target = package_dir / filename
        if target.exists() and _sha256(target.read_bytes()) == expected_hash:
            continue
        target.write_bytes(
            _download_verified(f"{IFEVAL_BASE_URL}/{filename}", expected_hash)
        )

    try:
        import nltk
    except ImportError as exc:
        raise RuntimeError(
            "缺少官方 IFEval verifier 依赖；请使用 "
            "`uv run --extra benchmark python tests/evals/run_official_ifeval.py`。"
        ) from exc

    for resource, package in (
        ("tokenizers/punkt", "punkt"),
        ("tokenizers/punkt_tab", "punkt_tab"),
    ):
        try:
            nltk.data.find(resource)
        except LookupError:
            nltk.download(package, quiet=True, raise_on_error=True)

    cache_path = str(cache_dir)
    if cache_path not in sys.path:
        sys.path.insert(0, cache_path)
    importlib.invalidate_caches()
    try:
        from instruction_following_eval import instructions_registry
    except ImportError as exc:
        raise RuntimeError(
            "官方 IFEval verifier 依赖不完整；请安装 benchmark extra。"
        ) from exc
    return instructions_registry


def _candidate_responses(response: str, *, loose: bool) -> list[str]:
    if not loose:
        return [response]
    lines = response.split("\n")
    remove_first = "\n".join(lines[1:]).strip()
    remove_last = "\n".join(lines[:-1]).strip()
    remove_both = "\n".join(lines[1:-1]).strip()
    variants = [response, remove_first, remove_last, remove_both]
    return variants + [variant.replace("*", "") for variant in variants]


def _verify_response(
    record: dict[str, Any],
    response: str,
    registry: Any,
    *,
    loose: bool,
) -> list[bool]:
    scores: list[bool] = []
    for index, instruction_id in enumerate(record["instruction_id_list"]):
        instruction_cls = registry.INSTRUCTION_DICT[instruction_id]
        instruction = instruction_cls(instruction_id)
        instruction.build_description(**record["kwargs"][index])
        args = instruction.get_instruction_args()
        if args and "prompt" in args:
            instruction.build_description(prompt=record["prompt"])
        passed = any(
            candidate.strip() and instruction.check_following(candidate)
            for candidate in _candidate_responses(response, loose=loose)
        )
        scores.append(bool(passed))
    return scores


class PinnedIFEval:
    def __init__(
        self,
        *,
        cache_dir: Path,
        n_problems: int | None = None,
        verbose_mode: bool = False,
        sample_seed: str = "open-eagle-ifeval-v1",
    ) -> None:
        self.cache_dir = cache_dir
        self.n_problems = n_problems
        self.verbose_mode = verbose_mode
        self.sample_seed = sample_seed

    def load_records(self) -> list[dict[str, Any]]:
        path = ensure_ifeval_data(self.cache_dir)
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if self.n_problems is not None and self.n_problems < len(records):
            return self._stratified_sample(records, self.n_problems)
        return records

    def _stratified_sample(
        self,
        records: list[dict[str, Any]],
        count: int,
    ) -> list[dict[str, Any]]:
        ranked = sorted(
            records,
            key=lambda record: hashlib.sha256(
                f"{self.sample_seed}:{record['key']}".encode()
            ).hexdigest(),
        )
        uncovered = {
            instruction_id
            for record in records
            for instruction_id in record["instruction_id_list"]
        }
        selected: list[dict[str, Any]] = []
        remaining = list(ranked)
        while remaining and len(selected) < count and uncovered:
            best = max(
                remaining,
                key=lambda record: len(
                    uncovered.intersection(record["instruction_id_list"])
                ),
            )
            selected.append(best)
            remaining.remove(best)
            uncovered.difference_update(best["instruction_id_list"])
        selected.extend(remaining[: count - len(selected)])
        return selected

    def evaluate(self, model: "OpenEagleIFEvalModel") -> IFEvalResult:
        registry = _ensure_official_verifier(self.cache_dir)
        records = self.load_records()
        cases: list[dict[str, Any]] = []
        breakdown_counts: dict[str, dict[str, int]] = {}
        strict_prompt_correct = 0
        loose_prompt_correct = 0
        strict_instruction_correct = 0
        loose_instruction_correct = 0
        instruction_total = 0

        for index, record in enumerate(records, start=1):
            response = model.generate(str(record["prompt"]))
            strict = _verify_response(record, response, registry, loose=False)
            loose = _verify_response(record, response, registry, loose=True)
            strict_prompt_correct += int(all(strict))
            loose_prompt_correct += int(all(loose))
            strict_instruction_correct += sum(strict)
            loose_instruction_correct += sum(loose)
            instruction_total += len(strict)

            for instruction_id, strict_score, loose_score in zip(
                record["instruction_id_list"], strict, loose
            ):
                counts = breakdown_counts.setdefault(
                    instruction_id,
                    {"total": 0, "strict": 0, "loose": 0},
                )
                counts["total"] += 1
                counts["strict"] += int(strict_score)
                counts["loose"] += int(loose_score)

            case_result = {
                "key": record["key"],
                "strict_pass": all(strict),
                "loose_pass": all(loose),
                "instruction_ids": record["instruction_id_list"],
                "strict_instruction_scores": strict,
                "loose_instruction_scores": loose,
                "response": response,
            }
            cases.append(case_result)
            if self.verbose_mode:
                print(json.dumps(case_result, ensure_ascii=False, indent=2))
            else:
                print(
                    f"IFEval {index}/{len(records)} key={record['key']} "
                    f"strict={all(strict)} loose={all(loose)}",
                    flush=True,
                )

        count = len(records)
        breakdown = {
            instruction_id: {
                "strict": values["strict"] / values["total"],
                "loose": values["loose"] / values["total"],
            }
            for instruction_id, values in sorted(breakdown_counts.items())
        }
        return IFEvalResult(
            strict_prompt_accuracy=strict_prompt_correct / count,
            loose_prompt_accuracy=loose_prompt_correct / count,
            strict_instruction_accuracy=strict_instruction_correct / instruction_total,
            loose_instruction_accuracy=loose_instruction_correct / instruction_total,
            instruction_breakdown=breakdown,
            cases=cases,
        )


class OpenEagleIFEvalModel:
    def generate(self, prompt: str) -> str:
        return asyncio.run(self._run(prompt))

    @staticmethod
    async def _run(prompt: str) -> str:
        run = await run_black_box_case(
            {
                "id": f"ifeval-{hashlib.sha256(prompt.encode()).hexdigest()[:12]}",
                "capability": "official_ifeval",
                "permission_mode": "all",
                "input": prompt,
                "files": {},
                "timeout_seconds": float(
                    os.environ.get("IFEVAL_CASE_TIMEOUT_SECONDS", "90")
                ),
            }
        )
        try:
            return run.final_output
        finally:
            run.cleanup()
