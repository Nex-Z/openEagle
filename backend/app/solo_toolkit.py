from __future__ import annotations

from typing import Any

from agno.tools import Toolkit

from .solo_executor import SoloExecutor


class SoloToolkit(Toolkit):
    def __init__(self, executor: SoloExecutor) -> None:
        self._executor = executor
        super().__init__(
            name="solo_toolkit",
            tools=[
                self.screenshot,
                self.click,
                self.double_click,
                self.right_click,
                self.move_mouse,
                self.scroll,
                self.type_text,
                self.press_keys,
                self.wait,
                self.execute_command,
            ],
            instructions=(
                "这些是桌面动作工具定义，执行前必须确认参数合法。"
                "鼠标坐标支持像素值，或 0~1 的归一化比例坐标。"
            ),
            add_instructions=True,
        )

    def screenshot(self) -> dict[str, Any]:
        return self._executor.capture_screenshot()

    def click(self, x: float | None = None, y: float | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if x is not None:
            args["x"] = x
        if y is not None:
            args["y"] = y
        return self._executor.execute_action("click", args)

    def double_click(self, x: float | None = None, y: float | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if x is not None:
            args["x"] = x
        if y is not None:
            args["y"] = y
        return self._executor.execute_action("double_click", args)

    def right_click(self, x: float | None = None, y: float | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if x is not None:
            args["x"] = x
        if y is not None:
            args["y"] = y
        return self._executor.execute_action("right_click", args)

    def move_mouse(self, x: float, y: float) -> dict[str, Any]:
        return self._executor.execute_action("move_mouse", {"x": x, "y": y})

    def scroll(self, delta: int = 0) -> dict[str, Any]:
        return self._executor.execute_action("scroll", {"delta": delta})

    def type_text(self, text: str) -> dict[str, Any]:
        return self._executor.execute_action("type_text", {"text": text})

    def press_keys(self, keys: list[str]) -> dict[str, Any]:
        return self._executor.execute_action("press_keys", {"keys": keys})

    def wait(self, ms: int = 800) -> dict[str, Any]:
        return self._executor.execute_action("wait", {"ms": ms})

    def execute_command(
        self,
        command: str,
        cwd: str = ".",
        timeout_ms: int = 30_000,
        tail: int = 120,
    ) -> dict[str, Any]:
        return self._executor.execute_action(
            "execute_command",
            {
                "command": command,
                "cwd": cwd,
                "timeout_ms": timeout_ms,
                "tail": tail,
            },
        )

    def execute(self, action: str, action_args: dict[str, Any]) -> dict[str, Any]:
        if action == "screenshot":
            return self.screenshot()
        if action == "click":
            return self.click(
                x=float(action_args["x"]) if "x" in action_args else None,
                y=float(action_args["y"]) if "y" in action_args else None,
            )
        if action == "double_click":
            return self.double_click(
                x=float(action_args["x"]) if "x" in action_args else None,
                y=float(action_args["y"]) if "y" in action_args else None,
            )
        if action == "right_click":
            return self.right_click(
                x=float(action_args["x"]) if "x" in action_args else None,
                y=float(action_args["y"]) if "y" in action_args else None,
            )
        if action == "move_mouse":
            return self.move_mouse(x=float(action_args["x"]), y=float(action_args["y"]))
        if action == "scroll":
            return self.scroll(delta=int(action_args.get("delta", 0)))
        if action == "type_text":
            return self.type_text(text=str(action_args.get("text", "")))
        if action == "press_keys":
            keys = action_args.get("keys", [])
            if not isinstance(keys, list):
                raise ValueError("press_keys requires keys as list")
            return self.press_keys(keys=[str(item) for item in keys])
        if action == "wait":
            return self.wait(ms=int(action_args.get("ms", 800)))
        if action == "execute_command":
            command = str(action_args.get("command", ""))
            cwd = str(action_args.get("cwd", "."))
            timeout_ms = int(action_args.get("timeout_ms", 30_000))
            tail = int(action_args.get("tail", 120))
            return self.execute_command(
                command=command,
                cwd=cwd,
                timeout_ms=timeout_ms,
                tail=tail,
            )
        raise ValueError(f"unsupported action: {action}")
