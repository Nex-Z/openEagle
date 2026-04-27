from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import utc_now


class SoloRunLogger:
    def __init__(self, workspace_root: Path) -> None:
        self._log_dir = workspace_root / ".open-eagle" / "solo-runs"
        self._path: Path | None = None

    @property
    def path(self) -> str | None:
        return self._path.as_posix() if self._path else None

    def start(self, request_id: str, task: str) -> str:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in request_id)
        self._path = self._log_dir / f"{safe_id}.jsonl"
        self.write("run_started", {"requestId": request_id, "task": task})
        return self._path.as_posix()

    def write(self, event: str, payload: dict[str, Any]) -> None:
        if self._path is None:
            return
        record = {
            "event": event,
            "timestamp": utc_now(),
            **payload,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")
