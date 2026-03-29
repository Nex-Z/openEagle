from __future__ import annotations

import time
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from typing import Any
from uuid import uuid4


class SoloExecutor:
    def __init__(self) -> None:
        self._pyautogui = None
        self._workspace_root = Path(__file__).resolve().parents[2]
        self._last_capture_region: dict[str, int] | None = None
        self._preferred_display_index = 1

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _normalize_point(value: float, bound: int, offset: int = 0) -> int:
        if 0 <= value <= 1:
            return int(value * bound) + offset
        raw = int(value)
        if offset != 0 and 0 <= raw <= bound:
            return raw + offset
        return raw

    @staticmethod
    def _normalize_key(token: str) -> str:
        mapping = {
            "control": "ctrl",
            "ctrl": "ctrl",
            "win": "win",
            "meta": "win",
            "cmd": "win",
            "return": "enter",
            "escape": "esc",
        }
        lowered = token.strip().lower()
        return mapping.get(lowered, lowered)

    def _ensure_pyautogui(self):  # type: ignore[no-untyped-def]
        if self._pyautogui is not None:
            return self._pyautogui
        try:
            import pyautogui  # type: ignore[import-untyped]
        except ModuleNotFoundError as exc:
            raise RuntimeError("缺少依赖 pyautogui，请先安装并重启后端。") from exc
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.03
        self._pyautogui = pyautogui
        return pyautogui

    def set_preferred_display_index(self, index: int) -> None:
        self._preferred_display_index = max(int(index), 1)

    def _pick_monitor(
        self,
        monitors: list[dict[str, Any]],
        preferred_index: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        physical_count = max(len(monitors) - 1, 0)
        if physical_count <= 0:
            return 0, monitors[0]

        wanted = self._preferred_display_index if preferred_index is None else int(preferred_index)
        normalized = min(max(wanted, 1), physical_count)
        return normalized, monitors[normalized]

    def _capture_monitor(
        self,
        sct: Any,
        monitor: dict[str, Any],
        filename_prefix: str,
    ) -> dict[str, Any]:
        from mss import tools as mss_tools  # type: ignore[import-untyped]

        frame = sct.grab(monitor)
        target = Path(gettempdir()) / f"{filename_prefix}_{uuid4().hex}.png"
        mss_tools.to_png(frame.rgb, frame.size, output=str(target))
        normalized_path = target.resolve().as_posix()
        return {
            "path": normalized_path,
            "width": int(frame.width),
            "height": int(frame.height),
            "left": int(monitor.get("left", 0)),
            "top": int(monitor.get("top", 0)),
            "capturedAt": self._now_iso(),
        }

    def list_displays(self, include_preview: bool = True) -> list[dict[str, Any]]:
        try:
            import mss  # type: ignore[import-untyped]
        except ModuleNotFoundError as exc:
            raise RuntimeError("缺少依赖 mss，请先安装并重启后端。") from exc

        with mss.mss() as sct:
            monitors = sct.monitors
            _, selected = self._pick_monitor(monitors)
            selected_left = int(selected.get("left", 0))
            selected_top = int(selected.get("top", 0))
            displays: list[dict[str, Any]] = []
            start_index = 1 if len(monitors) > 1 else 0
            for index in range(start_index, len(monitors)):
                monitor = monitors[index]
                info: dict[str, Any] = {
                    "index": index,
                    "label": f"显示器 {index}",
                    "left": int(monitor.get("left", 0)),
                    "top": int(monitor.get("top", 0)),
                    "width": int(monitor.get("width", 0)),
                    "height": int(monitor.get("height", 0)),
                    "isPrimary": index == 1,
                    "isSelected": int(monitor.get("left", 0)) == selected_left
                    and int(monitor.get("top", 0)) == selected_top,
                }
                if include_preview:
                    preview = self._capture_monitor(
                        sct,
                        monitor,
                        filename_prefix=f"open_eagle_display_{index}",
                    )
                    info["previewPath"] = preview["path"]
                    info["capturedAt"] = preview["capturedAt"]
                displays.append(info)

            if not displays:
                monitor = monitors[0]
                info = {
                    "index": 0,
                    "label": "显示器 0",
                    "left": int(monitor.get("left", 0)),
                    "top": int(monitor.get("top", 0)),
                    "width": int(monitor.get("width", 0)),
                    "height": int(monitor.get("height", 0)),
                    "isPrimary": True,
                    "isSelected": True,
                }
                if include_preview:
                    preview = self._capture_monitor(
                        sct,
                        monitor,
                        filename_prefix="open_eagle_display_0",
                    )
                    info["previewPath"] = preview["path"]
                    info["capturedAt"] = preview["capturedAt"]
                displays.append(info)

            return displays

    def capture_screenshot(self) -> dict[str, Any]:
        try:
            import mss  # type: ignore[import-untyped]
        except ModuleNotFoundError as exc:
            raise RuntimeError("缺少依赖 mss，请先安装并重启后端。") from exc

        with mss.mss() as sct:
            display_index, monitor = self._pick_monitor(sct.monitors)
            screenshot = self._capture_monitor(
                sct,
                monitor,
                filename_prefix=f"open_eagle_solo_display_{display_index}",
            )
            self._last_capture_region = {
                "left": int(screenshot["left"]),
                "top": int(screenshot["top"]),
                "width": int(screenshot["width"]),
                "height": int(screenshot["height"]),
            }
            screenshot["displayIndex"] = display_index
            return screenshot

    def _extract_xy(
        self,
        args: dict[str, Any],
        width: int,
        height: int,
        left: int = 0,
        top: int = 0,
    ) -> tuple[int, int] | None:
        x = args.get("x")
        y = args.get("y")
        if x is None or y is None:
            return None
        return self._normalize_point(float(x), width, left), self._normalize_point(
            float(y),
            height,
            top,
        )

    def execute_action(self, action: str, action_args: dict[str, Any]) -> dict[str, Any]:
        if action == "wait":
            wait_ms = max(int(action_args.get("ms", 800)), 150)
            time.sleep(wait_ms / 1000)
            return {"ok": True, "action": action, "waitMs": wait_ms}

        if action == "screenshot":
            screenshot = self.capture_screenshot()
            return {"ok": True, "action": action, "screenshot": screenshot}

        if action == "execute_command":
            command = action_args.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("execute_command requires non-empty command")

            cwd = action_args.get("cwd", ".")
            working_dir = (self._workspace_root / str(cwd)).resolve()
            try:
                working_dir.relative_to(self._workspace_root)
            except ValueError as exc:
                raise ValueError("cwd 超出工作区范围，不允许执行。") from exc
            if not working_dir.exists() or not working_dir.is_dir():
                raise ValueError("cwd 无效，必须是工作区内目录。")

            timeout_ms = int(action_args.get("timeout_ms", 30_000))
            timeout_s = max(1, min(timeout_ms / 1000, 120))
            tail = int(action_args.get("tail", 120))
            tail = max(1, min(tail, 300))

            completed = subprocess.run(
                command,
                cwd=str(working_dir),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            combined = stdout if completed.returncode == 0 else stderr or stdout
            if not combined:
                combined = "(no output)"
            lines = combined.splitlines()
            output = "\n".join(lines[-tail:])
            return {
                "ok": completed.returncode == 0,
                "action": action,
                "command": command,
                "cwd": str(working_dir),
                "exitCode": completed.returncode,
                "output": output,
            }

        pyautogui = self._ensure_pyautogui()
        if self._last_capture_region:
            width = self._last_capture_region["width"]
            height = self._last_capture_region["height"]
            left = self._last_capture_region["left"]
            top = self._last_capture_region["top"]
        else:
            screen_width, screen_height = pyautogui.size()
            width = int(screen_width)
            height = int(screen_height)
            left = 0
            top = 0
        point = self._extract_xy(action_args, width, height, left, top)

        if action == "click":
            if point:
                pyautogui.click(point[0], point[1])
            else:
                pyautogui.click()
            return {"ok": True, "action": action}

        if action == "double_click":
            if point:
                pyautogui.doubleClick(point[0], point[1])
            else:
                pyautogui.doubleClick()
            return {"ok": True, "action": action}

        if action == "right_click":
            if point:
                pyautogui.rightClick(point[0], point[1])
            else:
                pyautogui.rightClick()
            return {"ok": True, "action": action}

        if action == "move_mouse":
            if point is None:
                raise ValueError("move_mouse requires x and y")
            pyautogui.moveTo(point[0], point[1])
            return {"ok": True, "action": action, "x": point[0], "y": point[1]}

        if action == "scroll":
            delta = int(action_args.get("delta", 0))
            pyautogui.scroll(delta)
            return {"ok": True, "action": action, "delta": delta}

        if action == "type_text":
            text = action_args.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError("type_text requires text")
            pyautogui.typewrite(text)
            return {"ok": True, "action": action}

        if action == "press_keys":
            keys = action_args.get("keys")
            if not isinstance(keys, list) or not keys:
                raise ValueError("press_keys requires keys")
            normalized = [self._normalize_key(str(item)) for item in keys if str(item).strip()]
            if not normalized:
                raise ValueError("press_keys requires valid keys")
            if len(normalized) == 1:
                pyautogui.press(normalized[0])
            else:
                pyautogui.hotkey(*normalized)
            return {"ok": True, "action": action, "keys": normalized}

        raise ValueError(f"unsupported action: {action}")
