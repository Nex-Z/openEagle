from __future__ import annotations

import re
import hashlib
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agno.tools import Toolkit
from agno.tools.function import Function

from .attachments import AttachmentError, AttachmentStore
from .command_runner import DEFAULT_COMMAND_TAIL, DEFAULT_COMMAND_TIMEOUT_MS
from .command_runner import execute_workspace_command
from .config import ToolConfig
from .confirmations import PendingToolConfirmation, ToolConfirmationStore
from .safety import BlockedActionError, assess_tool_action, resolve_workspace_path

DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_SEARCH_RESULTS = 50
DEFAULT_MAX_SEARCH_FILE_BYTES = 2_000_000
DEFAULT_SHA256_SIZE_THRESHOLD = 1_000_000
DEFAULT_MAX_LIST_RESULTS = 200
DEFAULT_IGNORED_NAMES = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    ".open-eagle",
    ".idea",
}
DEFAULT_BINARY_EXTENSIONS = {
    ".7z",
    ".avif",
    ".bmp",
    ".class",
    ".dll",
    ".exe",
    ".gif",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".wasm",
    ".webp",
    ".zip",
}


def _truncate_text(text: str, max_chars: int) -> str:
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n\n...[truncated {omitted} chars]"


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_ignored_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in DEFAULT_IGNORED_NAMES for part in relative.parts)


def _iter_workspace_entries(base_dir: Path, root: Path):
    for current, dir_names, file_names in os.walk(base_dir):
        current_path = Path(current)
        dir_names[:] = [
            name
            for name in dir_names
            if not _is_ignored_path(current_path / name, root)
        ]
        for name in dir_names:
            yield current_path / name
        for name in file_names:
            candidate = current_path / name
            if not _is_ignored_path(candidate, root):
                yield candidate


def _iter_workspace_files(base_dir: Path, root: Path):
    for candidate in _iter_workspace_entries(base_dir, root):
        if candidate.is_file():
            yield candidate


def _normalize_globs(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _matches_any_glob(relative_path: str, globs: list[str]) -> bool:
    candidate = Path(relative_path)
    return any(candidate.match(pattern) or relative_path == pattern for pattern in globs)


def _is_probably_binary(path: Path) -> bool:
    if path.suffix.lower() in DEFAULT_BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return True
    return b"\0" in sample


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_confirmation_response(
    confirmation_store: ToolConfirmationStore | None,
    request_id: str | None,
    conversation_id: str | None,
    name: str,
    reason: str,
    params: dict[str, object],
) -> str:
    if not confirmation_store or not request_id or not conversation_id:
        return "Error: 当前工具需要确认，但确认通道未初始化。"
    pending = confirmation_store.create(
        request_id=request_id,
        conversation_id=conversation_id,
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


def _replace_text(path: Path, old_text: str, new_text: str, expected_occurrences: int) -> str:
    current = path.read_text(encoding="utf-8")
    occurrences = current.count(old_text)
    if occurrences != expected_occurrences:
        return (
            "Error: 替换命中次数不符合预期。"
            f" expected={expected_occurrences}, actual={occurrences}"
        )

    updated = current.replace(old_text, new_text, expected_occurrences)
    path.write_text(updated, encoding="utf-8")
    return f"Successfully replaced {occurrences} occurrence(s) in: {path}"


def _apply_text_edits(
    path: Path,
    edits: object,
    expected_sha256: str | None = None,
) -> str:
    if not isinstance(edits, list) or not edits:
        return "Error: edits 必须是非空列表。"

    try:
        current = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Error: 目标文件不是可按 UTF-8 读取的文本文件。"

    if expected_sha256 and _sha256_text(current) != expected_sha256:
        return "Error: expected_sha256 与当前文件内容不匹配，未写入。"

    normalized: list[tuple[str, str, int]] = []
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return f"Error: 第 {index} 个 edit 必须是对象。"
        old_text = str(edit.get("old_text", ""))
        new_text = str(edit.get("new_text", ""))
        try:
            expected_occurrences = int(edit.get("expected_occurrences", 1))
        except (TypeError, ValueError):
            return f"Error: 第 {index} 个 edit 的 expected_occurrences 必须是整数。"
        if not old_text:
            return f"Error: 第 {index} 个 edit 缺少 old_text。"
        if expected_occurrences < 1:
            return f"Error: 第 {index} 个 edit 的 expected_occurrences 必须 >= 1。"
        actual = current.count(old_text)
        if actual != expected_occurrences:
            return (
                f"Error: 第 {index} 个 edit 命中次数不符合预期。"
                f" expected={expected_occurrences}, actual={actual}"
            )
        normalized.append((old_text, new_text, expected_occurrences))

    updated = current
    for old_text, new_text, expected_occurrences in normalized:
        updated = updated.replace(old_text, new_text, expected_occurrences)

    path.write_text(updated, encoding="utf-8")
    return f"Successfully applied {len(normalized)} text edit(s) in: {path}"


def execute_confirmed_tool(
    workspace_root: Path,
    pending: PendingToolConfirmation,
    *,
    attachment_store: AttachmentStore | None = None,
    attachment_request_id: str | None = None,
) -> str:
    root = workspace_root.resolve()
    if pending.name == "attach_file_to_reply":
        if attachment_store is None:
            return "Error: 附件仓库未初始化。"
        path = str(pending.params.get("path", ""))
        display_name = str(pending.params.get("display_name", "") or "")
        attachment = attachment_store.register_generated_file(
            pending.conversation_id,
            attachment_request_id or pending.request_id,
            path,
            display_name or None,
        )
        return f"已登记回复附件: {attachment.name} ({attachment.size} bytes)"

    if pending.name == "write_text_file":
        path = str(pending.params.get("path", ""))
        content = str(pending.params.get("content", ""))
        target = resolve_workspace_path(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote UTF-8 file: {target}"

    if pending.name == "create_directory":
        path = str(pending.params.get("path", ""))
        target = resolve_workspace_path(root, path)
        target.mkdir(parents=True, exist_ok=True)
        return f"Successfully created directory: {target}"

    if pending.name == "copy_path":
        source = resolve_workspace_path(root, str(pending.params.get("source", "")))
        destination = resolve_workspace_path(root, str(pending.params.get("destination", "")))
        overwrite = bool(pending.params.get("overwrite", False))
        if destination.exists() and not overwrite:
            return f"Error: 目标已存在: {destination}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if destination.exists() and overwrite:
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        return f"Successfully copied: {source} -> {destination}"

    if pending.name == "move_path":
        source = resolve_workspace_path(root, str(pending.params.get("source", "")))
        destination = resolve_workspace_path(root, str(pending.params.get("destination", "")))
        overwrite = bool(pending.params.get("overwrite", False))
        if destination.exists() and not overwrite:
            return f"Error: 目标已存在: {destination}"
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return f"Successfully moved: {source} -> {destination}"

    if pending.name == "delete_path":
        target = resolve_workspace_path(root, str(pending.params.get("path", "")))
        recursive = bool(pending.params.get("recursive", False))
        if target.is_dir():
            if not recursive:
                return "Error: 目标是目录，必须 recursive=true 才能删除。"
            shutil.rmtree(target)
        else:
            target.unlink()
        return f"Successfully deleted: {target}"

    if pending.name == "replace_text_in_file":
        path = str(pending.params.get("path", ""))
        old_text = str(pending.params.get("old_text", ""))
        new_text = str(pending.params.get("new_text", ""))
        expected_occurrences = int(pending.params.get("expected_occurrences", 1))
        target = resolve_workspace_path(root, path)
        return _replace_text(target, old_text, new_text, expected_occurrences)

    if pending.name == "apply_text_edits":
        path = str(pending.params.get("path", ""))
        edits = pending.params.get("edits", [])
        expected_sha256 = str(pending.params.get("expected_sha256", "") or "")
        target = resolve_workspace_path(root, path)
        return _apply_text_edits(target, edits, expected_sha256 or None)

    if pending.name == "run_command":
        command = str(pending.params.get("command", ""))
        cwd = str(pending.params.get("cwd", "."))
        tail = int(pending.params.get("tail", DEFAULT_COMMAND_TAIL))
        timeout_ms = int(pending.params.get("timeout_ms", DEFAULT_COMMAND_TIMEOUT_MS))
        env = pending.params.get("env")
        if env and not isinstance(env, dict):
            env = None
        return execute_workspace_command(
            workspace_root=root,
            command=command,
            cwd=cwd,
            tail=tail,
            timeout_ms=timeout_ms,
            env=env,
        )

    if pending.params.get("toolId") and pending.params.get("command"):
        return execute_workspace_command(
            workspace_root=root,
            command=str(pending.params.get("command", "")),
            cwd=str(pending.params.get("cwd", ".")),
            tail=int(pending.params.get("tail", DEFAULT_COMMAND_TAIL)),
            timeout_ms=int(pending.params.get("timeout_ms", DEFAULT_COMMAND_TIMEOUT_MS)),
        )

    return f"Error: unsupported confirmed tool: {pending.name}"


_READ_CACHE_TTL = 5.0


class _ReadCache:
    def __init__(self, ttl: float = _READ_CACHE_TTL) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: str) -> None:
        self._store[key] = (time.time(), value)


class OpenEagleDefaultTools(Toolkit):
    def __init__(
        self,
        workspace_root: Path,
        confirmation_store: ToolConfirmationStore | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
        permission_mode: str = "default",
        builtin_tools: list[dict[str, object]] | None = None,
        attachment_store: AttachmentStore | None = None,
    ):
        self.workspace_root = workspace_root.resolve()
        self.confirmation_store = confirmation_store
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.permission_mode = permission_mode
        self.attachment_store = attachment_store
        self._read_cache = _ReadCache()

        enabled_builtins = {bt["id"]: bt.get("enabled", True) for bt in (builtin_tools or [])}

        tools = [
            self.get_current_time,
            self.get_file_info,
            self.list_directory,
            self.read_text_file,
            self.write_text_file,
            self.create_directory,
            self.copy_path,
            self.move_path,
            self.delete_path,
            self.search_files,
            self.search_text,
            self.replace_text_in_file,
            self.apply_text_edits,
            self.run_command,
            self.attach_file_to_reply,
        ]
        instructions_parts = [
            "你可以使用内置默认工具执行工作区内的常用操作：查看文件信息、浏览目录、"
            "读取文本文件、搜索文件名与文本、执行命令，以及在确认后创建目录、写入、"
            "复制、移动、删除或精确编辑文件。如需把生成的文件发回给用户，"
            "必须显式调用 attach_file_to_reply。"
        ]

        if enabled_builtins.get("web_search", True):
            tools.append(self.web_search)
            instructions_parts.append("你还可以使用 web_search 在互联网上搜索信息。")

        super().__init__(
            name="open_eagle_default_tools",
            tools=tools,
            instructions="".join(instructions_parts),
            add_instructions=True,
        )

    def _resolve_path(self, path: str = ".") -> Path:
        return resolve_workspace_path(self.workspace_root, path)

    def _create_confirmation(self, name: str, reason: str, params: dict[str, object]) -> str:
        return create_confirmation_response(
            confirmation_store=self.confirmation_store,
            request_id=self.request_id,
            conversation_id=self.conversation_id,
            name=name,
            reason=reason,
            params=params,
        )

    def _should_confirm(self) -> bool:
        return self.permission_mode != "all"

    def _run_guarded_tool(self, name: str, params: dict[str, object]) -> str:
        assessment = assess_tool_action(name, params, self.workspace_root)
        if assessment.level == "blocked":
            return f"Error: {assessment.reason}"
        if assessment.level == "confirm" and self._should_confirm():
            return self._create_confirmation(name, assessment.reason, params)
        return execute_confirmed_tool(
            self.workspace_root,
            PendingToolConfirmation(
                confirmation_id="auto",
                request_id=self.request_id or "auto",
                conversation_id=self.conversation_id or "auto",
                kind="tool",
                name=name,
                reason=assessment.reason,
                params=params,
            ),
        )

    def get_current_time(self) -> str:
        """返回当前系统日期和时间。

        Returns:
            str: 当前日期时间，格式如 "2026-04-28 15:30:45 (周一)"。
        """
        now = datetime.now()
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[now.weekday()]
        return now.strftime(f"%Y-%m-%d %H:%M:%S ({weekday})")

    def web_search(self, query: str, max_results: int = 5) -> str:
        """使用百度搜索互联网信息。

        Args:
            query: 搜索关键词。
            max_results: 返回结果数量，默认 5 条。

        Returns:
            str: 搜索结果列表，包含标题、摘要和链接。
        """
        try:
            from baidusearch.baidusearch import search
        except ImportError:
            return "错误：baidusearch 未安装，请运行 uv sync 安装依赖。"

        try:
            results = search(query, num_results=max(max_results, 1))
        except Exception as exc:
            return f"搜索出错：{exc}"

        if not results:
            return f"未找到与「{query}」相关的结果。"

        lines = [f"搜索「{query}」的结果：\n"]
        for i, item in enumerate(results, 1):
            title = item.get("title", "")
            body = item.get("abstract", item.get("body", ""))
            href = item.get("url", item.get("href", ""))
            lines.append(f"{i}. **{title}**\n   {body}\n   {href}\n")
        return "\n".join(lines)

    def get_file_info(self, path: str) -> str:
        """返回工作区内路径的基础信息。

        Args:
            path: 相对工作区根目录的路径。

        Returns:
            str: 路径类型、大小、更新时间和文件哈希等信息。
        """
        cache_key = f"file_info:{path}"
        cached = self._read_cache.get(cache_key)
        if cached is not None:
            return cached

        target = self._resolve_path(path)
        if not target.exists():
            return f"Error: 路径不存在: {target}"

        stat = target.stat()
        info = [
            f"path: {_relative_path(target, self.workspace_root)}",
            f"type: {'directory' if target.is_dir() else 'file'}",
            f"sizeBytes: {stat.st_size}",
            f"modifiedAt: {stat.st_mtime}",
            f"ignoredByDefault: {_is_ignored_path(target, self.workspace_root)}",
        ]
        if target.is_file():
            if stat.st_size <= DEFAULT_SHA256_SIZE_THRESHOLD:
                info.append(f"sha256: {_sha256_file(target)}")
            else:
                info.append("sha256: (skipped, file too large)")
        result = "\n".join(info)
        self._read_cache.set(cache_key, result)
        return result

    def list_directory(self, path: str = ".") -> str:
        """列出工作区内指定目录的文件和子目录。

        Args:
            path: 相对工作区根目录的路径，默认为当前工作区根目录。

        Returns:
            str: 目录内容列表，每行一个条目，目录以 / 结尾。
        """
        cache_key = f"list_dir:{path}"
        cached = self._read_cache.get(cache_key)
        if cached is not None:
            return cached

        target = self._resolve_path(path)
        if not target.exists():
            return f"Error: 路径不存在: {target}"
        if not target.is_dir():
            return f"Error: 目标不是目录: {target}"

        entries = []
        for item in sorted(target.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower())):
            if _is_ignored_path(item, self.workspace_root):
                continue
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{item.name}{suffix}")
        result = "\n".join(entries) if entries else "(empty)"
        self._read_cache.set(cache_key, result)
        return result

    def read_text_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        include_line_numbers: bool = False,
    ) -> str:
        """以 UTF-8 读取工作区内的文本文件。

        Args:
            path: 相对工作区根目录的文件路径。
            start_line: 可选起始行号，从 1 开始。
            end_line: 可选结束行号，包含该行。
            max_chars: 最多返回字符数，默认截断超长内容。
            include_line_numbers: 是否在每行前包含行号。

        Returns:
            str: 文件文本内容。
        """
        cache_key = f"read_file:{path}:{start_line}:{end_line}:{max_chars}:{include_line_numbers}"
        cached = self._read_cache.get(cache_key)
        if cached is not None:
            return cached

        target = self._resolve_path(path)
        if not target.exists():
            return f"Error: 文件不存在: {target}"
        if not target.is_file():
            return f"Error: 目标不是文件: {target}"

        if start_line is not None and start_line < 1:
            return "Error: start_line 必须 >= 1。"
        if end_line is not None and end_line < 1:
            return "Error: end_line 必须 >= 1。"
        if start_line is not None and end_line is not None and end_line < start_line:
            return "Error: end_line 不能小于 start_line。"
        if _is_probably_binary(target):
            return "Error: 目标看起来是二进制文件，不能作为 UTF-8 文本读取。"

        limit = max(1, int(max_chars))
        start = start_line or 1
        pieces: list[str] = []
        char_count = 0
        saw_requested_line = False
        truncated = False

        try:
            with target.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if line_number < start:
                        continue
                    if end_line is not None and line_number > end_line:
                        break
                    saw_requested_line = True
                    line = raw_line.rstrip("\r\n")
                    if include_line_numbers:
                        line = f"{line_number}: {line}"
                    prefix = "" if not pieces else "\n"
                    next_piece = f"{prefix}{line}"
                    remaining = limit - char_count
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(next_piece) > remaining:
                        pieces.append(next_piece[:remaining])
                        truncated = True
                        break
                    pieces.append(next_piece)
                    char_count += len(next_piece)
        except UnicodeDecodeError:
            return "Error: 目标文件不是有效 UTF-8 文本。"
        except OSError as exc:
            return f"Error: 读取文件失败: {exc}"

        if not saw_requested_line:
            return "(no content in requested line range)"

        content = "".join(pieces)
        if truncated:
            result = f"{content}\n\n...[truncated at max_chars={limit}]"
        else:
            result = content
        self._read_cache.set(cache_key, result)
        return result

    def write_text_file(self, path: str, content: str) -> str:
        """以 UTF-8 写入工作区内文本文件。

        Args:
            path: 相对工作区根目录的文件路径。
            content: 要写入的内容。

        Returns:
            str: 写入结果。
        """
        params = {"path": path, "content": content}
        return self._run_guarded_tool("write_text_file", params)

    def create_directory(self, path: str) -> str:
        """在工作区内创建目录。

        Args:
            path: 相对工作区根目录的目录路径。

        Returns:
            str: 创建结果。
        """
        return self._run_guarded_tool("create_directory", {"path": path})

    def copy_path(self, source: str, destination: str, overwrite: bool = False) -> str:
        """复制工作区内的文件或目录。

        Args:
            source: 源路径，相对工作区根目录。
            destination: 目标路径，相对工作区根目录。
            overwrite: 目标存在时是否覆盖。

        Returns:
            str: 复制结果。
        """
        return self._run_guarded_tool(
            "copy_path",
            {
                "source": source,
                "destination": destination,
                "overwrite": overwrite,
            },
        )

    def move_path(self, source: str, destination: str, overwrite: bool = False) -> str:
        """移动或重命名工作区内的文件或目录。

        Args:
            source: 源路径，相对工作区根目录。
            destination: 目标路径，相对工作区根目录。
            overwrite: 目标存在时是否覆盖。

        Returns:
            str: 移动结果。
        """
        return self._run_guarded_tool(
            "move_path",
            {
                "source": source,
                "destination": destination,
                "overwrite": overwrite,
            },
        )

    def delete_path(self, path: str, recursive: bool = False) -> str:
        """删除工作区内的文件或目录。

        Args:
            path: 相对工作区根目录的路径。
            recursive: 删除目录时是否递归删除。

        Returns:
            str: 删除结果。
        """
        return self._run_guarded_tool(
            "delete_path",
            {
                "path": path,
                "recursive": recursive,
            },
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
        if _is_ignored_path(base_dir, self.workspace_root):
            return "(ignored path)"
        if not keyword.strip():
            return "Error: keyword 不能为空。"

        keyword_lower = keyword.lower()
        matches: list[str] = []
        truncated = False
        for candidate in _iter_workspace_entries(base_dir, self.workspace_root):
            if keyword_lower in candidate.name.lower():
                matches.append(_relative_path(candidate, self.workspace_root))
                if len(matches) >= DEFAULT_MAX_LIST_RESULTS:
                    truncated = True
                    break
        if not matches:
            return "(no matches)"
        result = "\n".join(matches)
        if truncated:
            result += f"\n...[truncated at max_results={DEFAULT_MAX_LIST_RESULTS}]"
        return result

    def search_text(
        self,
        query: str,
        path: str = ".",
        max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
        include_globs: list[str] | str | None = None,
        exclude_globs: list[str] | str | None = None,
        case_sensitive: bool = False,
    ) -> str:
        """在工作区内按文本内容搜索。

        Args:
            query: 要搜索的文本。
            path: 搜索起始目录，相对工作区根目录。
            max_results: 最多返回多少条匹配记录。
            include_globs: 可选包含 glob 过滤，只搜索匹配的文件。
                支持 Path.match() 语法，如 "*.ts"、"src/**/*.tsx"、"?*.test.*"。
                可传字符串或列表，如 ["*.ts", "*.tsx"]。
            exclude_globs: 可选排除 glob 过滤，跳过匹配的文件。语法同上。
            case_sensitive: 是否区分大小写。

        Returns:
            str: 命中的相对路径、行号和文本片段。
        """
        base_dir = self._resolve_path(path)
        if not base_dir.exists():
            return f"Error: 路径不存在: {base_dir}"
        if not base_dir.is_dir():
            return f"Error: 目标不是目录: {base_dir}"
        if _is_ignored_path(base_dir, self.workspace_root):
            return "(ignored path)"
        if not query.strip():
            return "Error: query 不能为空。"

        include_patterns = _normalize_globs(include_globs)
        exclude_patterns = _normalize_globs(exclude_globs)
        needle = query if case_sensitive else query.lower()
        limit = max(1, min(int(max_results), 200))
        matches: list[str] = []
        skipped_large = 0
        skipped_binary = 0
        truncated = False
        limit_reached = False

        for candidate in _iter_workspace_files(base_dir, self.workspace_root):
            if limit_reached:
                break
            relative = _relative_path(candidate, self.workspace_root)
            if include_patterns and not _matches_any_glob(relative, include_patterns):
                continue
            if exclude_patterns and _matches_any_glob(relative, exclude_patterns):
                continue

            try:
                if candidate.stat().st_size > DEFAULT_MAX_SEARCH_FILE_BYTES:
                    skipped_large += 1
                    continue
            except OSError:
                continue
            if _is_probably_binary(candidate):
                skipped_binary += 1
                continue

            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        haystack = line if case_sensitive else line.lower()
                        if needle in haystack:
                            matches.append(f"{relative}:{line_number}: {line.strip()}")
                            if len(matches) >= limit:
                                truncated = True
                                limit_reached = True
                                break
            except UnicodeDecodeError:
                skipped_binary += 1
                continue
            except OSError:
                continue

        if not matches:
            notes = []
            if skipped_large:
                notes.append(f"skipped_large={skipped_large}")
            if skipped_binary:
                notes.append(f"skipped_binary={skipped_binary}")
            return "(no matches)" if not notes else f"(no matches; {', '.join(notes)})"

        result = "\n".join(matches)
        notes = []
        if truncated:
            notes.append(f"truncated at max_results={limit}")
        if skipped_large:
            notes.append(f"skipped_large={skipped_large}")
        if skipped_binary:
            notes.append(f"skipped_binary={skipped_binary}")
        if notes:
            result += "\n...[" + ", ".join(notes) + "]"
        return result

    def replace_text_in_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_occurrences: int = 1,
    ) -> str:
        """在单个工作区文件中做精确文本替换。

        Args:
            path: 相对工作区根目录的文件路径。
            old_text: 要被替换的原始文本。
            new_text: 新文本。
            expected_occurrences: 预期命中次数，默认 1。

        Returns:
            str: 替换结果。
        """
        params = {
            "path": path,
            "old_text": old_text,
            "new_text": new_text,
            "expected_occurrences": expected_occurrences,
        }
        return self._run_guarded_tool("replace_text_in_file", params)

    def apply_text_edits(
        self,
        path: str,
        edits: list[dict[str, object]],
        expected_sha256: str | None = None,
    ) -> str:
        """在单个工作区文件中一次应用多段精确文本替换。

        Args:
            path: 相对工作区根目录的文件路径。
            edits: 替换列表，每项包含 old_text、new_text、expected_occurrences。
            expected_sha256: 可选当前 UTF-8 文本 sha256，用于防止并发覆盖。

        Returns:
            str: 编辑结果。
        """
        return self._run_guarded_tool(
            "apply_text_edits",
            {
                "path": path,
                "edits": edits,
                "expected_sha256": expected_sha256 or "",
            },
        )

    def run_command(
        self,
        command: str,
        cwd: str = ".",
        tail: int = DEFAULT_COMMAND_TAIL,
        timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS,
        env: dict[str, str] | None = None,
    ) -> str:
        """在工作区内执行命令并返回输出。

        Args:
            command: 要执行的命令字符串。
            cwd: 命令执行目录，相对工作区根目录。
            tail: 最多返回输出的最后多少行。
            timeout_ms: 命令超时毫秒数。
            env: 可选的环境变量字典，会与当前环境合并。

        Returns:
            str: 命令输出或错误信息。
        """
        params = {
            "command": command,
            "cwd": cwd,
            "tail": tail,
            "timeout_ms": timeout_ms,
        }
        if env:
            params["env"] = env
        return self._run_guarded_tool("run_command", params)

    def attach_file_to_reply(self, path: str, display_name: str = "") -> str:
        """将工作区内文件登记为本轮回复附件。

        Args:
            path: 要回传的文件路径，必须位于工作区内。
            display_name: 可选的附件展示名。

        Returns:
            str: 登记结果。
        """
        if self.attachment_store is None:
            return "Error: 附件仓库未初始化。"
        if not self.conversation_id or not self.request_id:
            return "Error: 当前请求缺少 conversation_id 或 request_id。"
        if self.conversation_id.startswith("im_") and self._should_confirm():
            return self._create_confirmation(
                "attach_file_to_reply",
                "将把工作区文件作为附件发送到远程 IM，需要确认。",
                {"path": path, "display_name": display_name},
            )
        try:
            attachment = self.attachment_store.register_generated_file(
                self.conversation_id,
                self.request_id,
                path,
                display_name or None,
            )
        except (AttachmentError, BlockedActionError) as exc:
            return f"Error: {exc}"
        return f"已登记回复附件: {attachment.name} ({attachment.size} bytes)"


def build_default_tools(
    workspace_root: Optional[Path] = None,
    confirmation_store: ToolConfirmationStore | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    permission_mode: str = "default",
    builtin_tools: list[dict[str, object]] | None = None,
    attachment_store: AttachmentStore | None = None,
) -> OpenEagleDefaultTools:
    root = workspace_root or Path(__file__).resolve().parents[2]
    return OpenEagleDefaultTools(
        workspace_root=root,
        confirmation_store=confirmation_store,
        request_id=request_id,
        conversation_id=conversation_id,
        permission_mode=permission_mode,
        builtin_tools=builtin_tools,
        attachment_store=attachment_store,
    )


def _build_configured_tool_name(tool: ToolConfig) -> str:
    safe_name = re.sub(r"[^0-9A-Za-z_-]+", "_", tool.name).strip("_").lower() or "custom"
    safe_id = re.sub(r"[^0-9A-Za-z]+", "", tool.id).lower() or "tool"
    suffix = safe_id[:8]
    max_name_length = 64 - len("tool__") - len(suffix)
    truncated_name = safe_name[:max_name_length].strip("_") or "custom"
    return f"tool_{truncated_name}_{suffix}"


def build_configured_tool_functions(
    tool_configs: list[ToolConfig],
    workspace_root: Optional[Path] = None,
    confirmation_store: ToolConfirmationStore | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    permission_mode: str = "default",
) -> tuple[list[Function], dict[str, str]]:
    root = (workspace_root or Path(__file__).resolve().parents[2]).resolve()
    functions: list[Function] = []
    name_map: dict[str, str] = {}

    for tool in tool_configs:
        if not tool.enabled or not tool.command.strip():
            continue

        function_name = _build_configured_tool_name(tool)
        display_name = tool.name.strip() or "未命名工具"
        working_dir = tool.cwd.strip() or "."
        timeout_ms = int(tool.timeout_ms)
        tail = int(tool.tail)

        placeholders = re.findall(r"\{(\w+)\}", tool.command)
        has_params = bool(placeholders)

        def make_configured_entrypoint(
            command: str,
            cwd: str,
            timeout_ms: int,
            tail: int,
            display_name: str,
            tool_id: str,
            param_names: list[str],
        ):
            if param_names:
                def run_configured_tool(**kwargs: str) -> str:
                    resolved_command = command
                    for name in param_names:
                        value = str(kwargs.get(name, ""))
                        if not value:
                            return f"Error: 参数 {name} 不能为空。"
                        resolved_command = resolved_command.replace(f"{{{name}}}", value)
                    params = {
                        "toolId": tool_id,
                        "toolName": display_name,
                        "command": resolved_command,
                        "cwd": cwd,
                        "timeout_ms": timeout_ms,
                        "tail": tail,
                    }
                    assessment = assess_tool_action("configured_tool", params, root)
                    if assessment.level == "blocked":
                        return f"Error: {assessment.reason}"
                    if assessment.level == "confirm" and permission_mode != "all":
                        return create_confirmation_response(
                            confirmation_store=confirmation_store,
                            request_id=request_id,
                            conversation_id=conversation_id,
                            name=display_name,
                            reason=assessment.reason,
                            params=params,
                        )
                    return execute_confirmed_tool(
                        root,
                        PendingToolConfirmation(
                            confirmation_id="auto",
                            request_id=request_id or "auto",
                            conversation_id=conversation_id or "auto",
                            kind="tool",
                            name=display_name,
                            reason=assessment.reason,
                            params=params,
                        ),
                    )
            else:
                def run_configured_tool() -> str:
                    params = {
                        "toolId": tool_id,
                        "toolName": display_name,
                        "command": command,
                        "cwd": cwd,
                        "timeout_ms": timeout_ms,
                        "tail": tail,
                    }
                    assessment = assess_tool_action("configured_tool", params, root)
                    if assessment.level == "blocked":
                        return f"Error: {assessment.reason}"
                    if assessment.level == "confirm" and permission_mode != "all":
                        return create_confirmation_response(
                            confirmation_store=confirmation_store,
                            request_id=request_id,
                            conversation_id=conversation_id,
                            name=display_name,
                            reason=assessment.reason,
                            params=params,
                        )
                    return execute_confirmed_tool(
                        root,
                        PendingToolConfirmation(
                            confirmation_id="auto",
                            request_id=request_id or "auto",
                            conversation_id=conversation_id or "auto",
                            kind="tool",
                            name=display_name,
                            reason=assessment.reason,
                            params=params,
                        ),
                    )

            return run_configured_tool

        if has_params:
            param_desc = "，".join(f"{p}: 字符串参数" for p in placeholders)
            description_parts = [
                f"参数化命令工具「{display_name}」，参数: {param_desc}",
                f"命令模板: {tool.command}",
                f"工作目录: {working_dir}，超时: {timeout_ms}ms，输出尾部 {tail} 行。",
                "命令经过工作区边界检查和风险分级。",
            ]
        else:
            description_parts = [
                f"固定命令工具「{display_name}」，无参数，执行: {tool.command}",
                f"工作目录: {working_dir}，超时: {timeout_ms}ms，输出尾部 {tail} 行。",
                "命令经过工作区边界检查和风险分级。",
            ]
        if tool.description.strip():
            description_parts.insert(1, f"说明: {tool.description.strip()}.")

        entrypoint = make_configured_entrypoint(
            command=tool.command,
            cwd=working_dir,
            timeout_ms=timeout_ms,
            tail=tail,
            display_name=display_name,
            tool_id=tool.id,
            param_names=placeholders,
        )
        if has_params:
            import inspect
            sig = inspect.signature(entrypoint)
            entrypoint.__doc__ = f"执行命令: {tool.command}"

        functions.append(
            Function(
                name=function_name,
                description=" ".join(description_parts),
                entrypoint=entrypoint,
            )
        )
        name_map[function_name] = display_name

    return functions, name_map
