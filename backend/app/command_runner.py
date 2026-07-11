from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .safety import resolve_workspace_path

DEFAULT_COMMAND_TIMEOUT_MS = 30_000
DEFAULT_COMMAND_TAIL = 120


@dataclass(frozen=True)
class WorkspaceCommandResult:
    output: str
    returncode: int | None
    timed_out: bool = False


def normalize_tail(tail: int) -> int:
    return max(1, min(int(tail), 300))


def normalize_timeout_ms(timeout_ms: int) -> int:
    return max(1_000, min(int(timeout_ms), 120_000))


def tail_output(
    stdout: str | None,
    stderr: str | None,
    returncode: int | None,
    tail: int,
    error_prefix: str | None = None,
    timed_out: bool = False,
) -> str:
    prefer_stdout = returncode == 0 and error_prefix is None and not timed_out
    combined = (stdout if prefer_stdout else stderr or stdout or "").strip()
    if not combined:
        combined = "(no output)"

    lines = combined.splitlines()
    tail_lines = "\n".join(lines[-normalize_tail(tail) :])

    if timed_out:
        return f"[TIMEOUT] Command timed out.\n{tail_lines}"
    if error_prefix:
        return f"{error_prefix}\n{tail_lines}"
    if returncode is not None and returncode != 0:
        return f"Error (exit {returncode}):\n{tail_lines}"
    return tail_lines


def run_workspace_command(
    workspace_root: Path,
    command: str,
    cwd: str = ".",
    tail: int = DEFAULT_COMMAND_TAIL,
    timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS,
    env: dict[str, str] | None = None,
    allow_external_paths: bool = False,
) -> WorkspaceCommandResult:
    working_dir = resolve_workspace_path(
        workspace_root.resolve(),
        cwd,
        allow_external_paths=allow_external_paths,
    )
    if not working_dir.exists() or not working_dir.is_dir():
        return WorkspaceCommandResult(f"Error: 无效执行目录: {working_dir}", None)

    import os
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        completed = subprocess.run(
            command,
            cwd=str(working_dir),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=normalize_timeout_ms(timeout_ms) / 1000,
            env=run_env,
        )
    except subprocess.TimeoutExpired as exc:
        output = tail_output(
            exc.stdout,
            exc.stderr,
            None,
            tail,
            timed_out=True,
        )
        return WorkspaceCommandResult(output, None, timed_out=True)

    output = tail_output(completed.stdout, completed.stderr, completed.returncode, tail)
    return WorkspaceCommandResult(output, completed.returncode)


def execute_workspace_command(
    workspace_root: Path,
    command: str,
    cwd: str = ".",
    tail: int = DEFAULT_COMMAND_TAIL,
    timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS,
    env: dict[str, str] | None = None,
    allow_external_paths: bool = False,
) -> str:
    return run_workspace_command(
        workspace_root=workspace_root,
        command=command,
        cwd=cwd,
        tail=tail,
        timeout_ms=timeout_ms,
        env=env,
        allow_external_paths=allow_external_paths,
    ).output
