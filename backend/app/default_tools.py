from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from agno.tools import Toolkit

from .confirmations import PendingToolConfirmation, ToolConfirmationStore
from .safety import assess_tool_action, resolve_workspace_path


def _tail_output(stdout: str, stderr: str, returncode: int, tail: int) -> str:
    combined = stdout if returncode == 0 else stderr or stdout
    if not combined:
        combined = "(no output)"
    lines = combined.strip().splitlines()
    tail_lines = "\n".join(lines[-max(tail, 1) :])
    if returncode != 0:
        return f"Error (exit {returncode}):\n{tail_lines}"
    return tail_lines


def execute_confirmed_tool(workspace_root: Path, pending: PendingToolConfirmation) -> str:
    root = workspace_root.resolve()
    if pending.name == "write_text_file":
        path = str(pending.params.get("path", ""))
        content = str(pending.params.get("content", ""))
        target = resolve_workspace_path(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote UTF-8 file: {target}"

    if pending.name == "run_command":
        command = str(pending.params.get("command", ""))
        cwd = str(pending.params.get("cwd", "."))
        tail = int(pending.params.get("tail", 120))
        working_dir = resolve_workspace_path(root, cwd)
        if not working_dir.exists() or not working_dir.is_dir():
            return f"Error: 无效执行目录: {working_dir}"

        completed = subprocess.run(
            command,
            cwd=str(working_dir),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return _tail_output(completed.stdout, completed.stderr, completed.returncode, tail)

    return f"Error: unsupported confirmed tool: {pending.name}"


class OpenEagleDefaultTools(Toolkit):
    def __init__(
        self,
        workspace_root: Path,
        confirmation_store: ToolConfirmationStore | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
    ):
        self.workspace_root = workspace_root.resolve()
        self.confirmation_store = confirmation_store
        self.request_id = request_id
        self.conversation_id = conversation_id
        super().__init__(
            name="open_eagle_default_tools",
            tools=[
                self.list_directory,
                self.read_text_file,
                self.write_text_file,
                self.search_files,
                self.run_command,
            ],
            instructions=(
                "你可以使用内置默认工具执行工作区内的常用操作：浏览目录、读取文本文件、"
                "写入 UTF-8 文本文件、按文件名搜索、执行命令。"
                "除非确有必要，不要修改工作区外的内容。"
            ),
            add_instructions=True,
        )

    def _resolve_path(self, path: str = ".") -> Path:
        return resolve_workspace_path(self.workspace_root, path)

    def _create_confirmation(self, name: str, reason: str, params: dict[str, object]) -> str:
        if not self.confirmation_store or not self.request_id or not self.conversation_id:
            return "Error: 当前工具需要确认，但确认通道未初始化。"
        pending = self.confirmation_store.create(
            request_id=self.request_id,
            conversation_id=self.conversation_id,
            kind="tool",
            name=name,
            reason=reason,
            params=params,
        )
        return (
            "CONFIRMATION_REQUIRED "
            f"{pending.confirmation_id}: {reason}。"
            "请等待用户在 openEagle 中允许或拒绝后再继续。"
        )

    def list_directory(self, path: str = ".") -> str:
        """列出工作区内指定目录的文件和子目录。

        Args:
            path: 相对工作区根目录的路径，默认为当前工作区根目录。

        Returns:
            str: 目录内容列表，每行一个条目，目录以 / 结尾。
        """
        target = self._resolve_path(path)
        if not target.exists():
            return f"Error: 路径不存在: {target}"
        if not target.is_dir():
            return f"Error: 目标不是目录: {target}"

        entries = []
        for item in sorted(target.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower())):
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{item.name}{suffix}")
        return "\n".join(entries) if entries else "(empty)"

    def read_text_file(self, path: str) -> str:
        """以 UTF-8 读取工作区内的文本文件。

        Args:
            path: 相对工作区根目录的文件路径。

        Returns:
            str: 文件文本内容。
        """
        target = self._resolve_path(path)
        if not target.exists():
            return f"Error: 文件不存在: {target}"
        if not target.is_file():
            return f"Error: 目标不是文件: {target}"
        return target.read_text(encoding="utf-8")

    def write_text_file(self, path: str, content: str) -> str:
        """以 UTF-8 写入工作区内文本文件。

        Args:
            path: 相对工作区根目录的文件路径。
            content: 要写入的内容。

        Returns:
            str: 写入结果。
        """
        params = {"path": path, "content": content}
        assessment = assess_tool_action("write_text_file", params, self.workspace_root)
        if assessment.level == "blocked":
            return f"Error: {assessment.reason}"
        if assessment.level == "confirm":
            return self._create_confirmation("write_text_file", assessment.reason, params)
        return execute_confirmed_tool(
            self.workspace_root,
            PendingToolConfirmation(
                confirmation_id="auto",
                request_id=self.request_id or "auto",
                conversation_id=self.conversation_id or "auto",
                kind="tool",
                name="write_text_file",
                reason=assessment.reason,
                params=params,
            ),
        )

    def search_files(self, keyword: str, path: str = ".") -> str:
        """在工作区内按文件名搜索。

        Args:
            keyword: 要匹配的关键词，大小写不敏感。
            path: 搜索起始目录，相对工作区根目录。

        Returns:
            str: 匹配到的相对路径列表。
        """
        base_dir = self._resolve_path(path)
        if not base_dir.exists():
            return f"Error: 路径不存在: {base_dir}"
        if not base_dir.is_dir():
            return f"Error: 目标不是目录: {base_dir}"

        keyword_lower = keyword.lower()
        matches = []
        for candidate in base_dir.rglob("*"):
            if keyword_lower in candidate.name.lower():
                matches.append(str(candidate.relative_to(self.workspace_root)))
        return "\n".join(matches[:200]) if matches else "(no matches)"

    def run_command(self, command: str, cwd: str = ".", tail: int = 120) -> str:
        """在工作区内执行命令并返回输出。

        Args:
            command: 要执行的命令字符串。
            cwd: 命令执行目录，相对工作区根目录。
            tail: 最多返回输出的最后多少行。

        Returns:
            str: 命令输出或错误信息。
        """
        params = {"command": command, "cwd": cwd, "tail": tail}
        assessment = assess_tool_action("run_command", params, self.workspace_root)
        if assessment.level == "blocked":
            return f"Error: {assessment.reason}"
        if assessment.level == "confirm":
            return self._create_confirmation("run_command", assessment.reason, params)
        return execute_confirmed_tool(
            self.workspace_root,
            PendingToolConfirmation(
                confirmation_id="auto",
                request_id=self.request_id or "auto",
                conversation_id=self.conversation_id or "auto",
                kind="tool",
                name="run_command",
                reason=assessment.reason,
                params=params,
            ),
        )


def build_default_tools(
    workspace_root: Optional[Path] = None,
    confirmation_store: ToolConfirmationStore | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
) -> OpenEagleDefaultTools:
    root = workspace_root or Path(__file__).resolve().parents[2]
    return OpenEagleDefaultTools(
        workspace_root=root,
        confirmation_store=confirmation_store,
        request_id=request_id,
        conversation_id=conversation_id,
    )
