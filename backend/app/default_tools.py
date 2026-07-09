from __future__ import annotations

import re
import inspect
import json
import hashlib
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from langchain_core.tools import BaseTool, StructuredTool

from .attachments import AttachmentError, AttachmentStore
from .command_runner import DEFAULT_COMMAND_TAIL, DEFAULT_COMMAND_TIMEOUT_MS
from .command_runner import execute_workspace_command
from .config import ToolConfig, WebSearchConfig
from .confirmations import PendingToolConfirmation, ToolConfirmationStore
from .paths import resolve_workspace_root
from .safety import (
    BlockedActionError,
    assess_contextual_tool_action,
    assess_tool_action,
    resolve_workspace_path,
)
from .scheduler.tools import create_scheduled_task

DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_SEARCH_RESULTS = 50
DEFAULT_MAX_SEARCH_FILE_BYTES = 2_000_000
DEFAULT_SHA256_SIZE_THRESHOLD = 1_000_000
DEFAULT_MAX_LIST_RESULTS = 200
MAX_WEB_SEARCH_BATCH_QUERIES = 5
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


def _tool_from_callable(fn: Any, *, name: str | None = None, description: str | None = None) -> BaseTool:
    return StructuredTool.from_function(
        func=fn,
        name=name or fn.__name__,
        description=description or inspect.getdoc(fn) or fn.__name__,
    )


class OpenEagleDefaultTools:
    def __init__(
        self,
        workspace_root: Path,
        confirmation_store: ToolConfirmationStore | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
        permission_mode: str = "default",
        builtin_tools: list[dict[str, object]] | None = None,
        web_search_config: WebSearchConfig | None = None,
        attachment_store: AttachmentStore | None = None,
        memory_service: Any | None = None,
        task_context: str | None = None,
    ):
        self.workspace_root = workspace_root.resolve()
        self.confirmation_store = confirmation_store
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.permission_mode = permission_mode
        self.attachment_store = attachment_store
        self.memory_service = memory_service
        self.task_context = task_context or ""
        self.web_search_config = web_search_config or WebSearchConfig()
        self._read_cache = _ReadCache()
        self._agent_tools: list[BaseTool] = []

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
            self.create_scheduled_task,
        ]
        if self.memory_service is not None:
            tools.extend(
                [
                    self.get_memory_state,
                    self.save_memory_note,
                    self.update_memory_note,
                    self.delete_memory_note,
                    self.save_user_profile,
                    self.save_soul_core,
                    self.save_agent_side_notes,
                ]
            )
        instructions_parts = [
            "你可以使用内置默认工具执行工作区内的常用操作：查看文件信息、浏览目录、"
            "读取文本文件、搜索文件名与文本、执行命令，以及在确认后创建目录、写入、"
            "复制、移动、删除或精确编辑文件。如需把生成的文件发回给用户，"
            "必须显式调用 attach_file_to_reply。"
        ]
        if self.memory_service is not None:
            instructions_parts.append(
                "当用户要求记住、记一下、记下、记录、以后记得、加入用户笔记、更新用户画像、更新 Soul 或保存旁注时，"
                "必须使用 get_memory_state/save_memory_note/update_memory_note/delete_memory_note/"
                "save_user_profile/save_soul_core/save_agent_side_notes 写入 openEagle Memory；不要用 write_text_file "
                "在项目根目录创建记忆文件。"
            )

        if (
            enabled_builtins.get("web_search", True)
            and self.web_search_config.provider != "disabled"
        ):
            tools.append(self.web_search)
            instructions_parts.append(
                "你还可以使用 web_search 通过 Tavily 在互联网上搜索信息；"
                "query 可传单个字符串，或传入最多 5 个互补查询组成的字符串数组并行搜索。"
            )

        self.name = "open_eagle_default_tools"
        self.instructions = "".join(instructions_parts)
        self._agent_tools = [_tool_from_callable(tool) for tool in tools]

    @property
    def agent_tools(self) -> list[BaseTool]:
        return list(self._agent_tools)

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
        contextual_assessment = assess_contextual_tool_action(name, params, self.task_context)
        if contextual_assessment is not None:
            return f"Error: {contextual_assessment.reason}"
        assessment = assess_tool_action(name, params, self.workspace_root)
        if assessment.level == "blocked" and not (assessment.overridable and self.permission_mode == "all"):
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

    def web_search(self, query: str | list[str], max_results: int | None = None) -> str:
        """使用 Tavily 搜索互联网信息。

        Args:
            query: 单个搜索关键词，或最多 5 个互补搜索关键词组成的数组。
            max_results: 返回结果数量；不传时使用设置中的默认值。

        Returns:
            str: 单个或批量搜索结果，包含标题、摘要和链接。
        """
        config = self.web_search_config
        if config.provider == "disabled":
            return "错误：内置联网搜索已关闭，请在「设置 → 联网搜索」中启用。"

        api_key = (config.api_key or os.getenv("TAVILY_API_KEY") or "").strip()
        if not api_key:
            return (
                "错误：尚未配置 Tavily API Key。"
                "请在「设置 → 联网搜索」中填写，或设置 TAVILY_API_KEY 环境变量。"
            )

        raw_queries = query if isinstance(query, list) else [query]
        clean_queries = list(
            dict.fromkeys(
                item.strip()
                for item in raw_queries
                if isinstance(item, str) and item.strip()
            )
        )
        if not clean_queries:
            return "错误：搜索关键词不能为空。"
        batch_truncated = len(clean_queries) > MAX_WEB_SEARCH_BATCH_QUERIES
        clean_queries = clean_queries[:MAX_WEB_SEARCH_BATCH_QUERIES]

        result_limit = config.max_results if max_results is None else max_results
        try:
            result_limit = max(1, min(int(result_limit), 20))
        except (TypeError, ValueError):
            return "错误：max_results 必须是 1 到 20 之间的整数。"

        if len(clean_queries) == 1:
            return self._web_search_one(clean_queries[0], api_key, result_limit)

        with ThreadPoolExecutor(max_workers=len(clean_queries)) as executor:
            results = list(
                executor.map(
                    lambda item: self._web_search_one(item, api_key, result_limit),
                    clean_queries,
                )
            )
        prefix = (
            f"批量搜索最多支持 {MAX_WEB_SEARCH_BATCH_QUERIES} 个查询，"
            "已忽略超出部分。\n\n"
            if batch_truncated
            else ""
        )
        sections = [
            f"## 批量查询 {index}: {item}\n{result}"
            for index, (item, result) in enumerate(zip(clean_queries, results), start=1)
        ]
        return prefix + "\n\n".join(sections)

    def _web_search_one(self, query: str, api_key: str, result_limit: int) -> str:
        config = self.web_search_config
        try:
            response = httpx.post(
                "https://api.tavily.com/search",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "topic": "general",
                    "search_depth": config.search_depth,
                    "max_results": result_limit,
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                },
                timeout=20.0,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                return "搜索出错：Tavily API Key 无效或无权访问。"
            return f"搜索出错：Tavily 返回 HTTP {exc.response.status_code}。"
        except (httpx.HTTPError, ValueError) as exc:
            return f"搜索出错：{exc}"

        results = payload.get("results") if isinstance(payload, dict) else None

        if not isinstance(results, list) or not results:
            return f"未找到与「{query}」相关的结果。"

        lines = [f"Tavily 搜索「{query}」的结果：\n"]
        for i, item in enumerate(results, 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "未命名结果")
            body = str(item.get("content") or "")
            href = str(item.get("url") or "")
            published_date = str(item.get("published_date") or "").strip()
            date_line = f"\n   发布时间：{published_date}" if published_date else ""
            lines.append(f"{i}. **{title}**\n   {body}{date_line}\n   {href}\n")
        if len(lines) == 1:
            return f"未找到与「{query}」相关的有效结果。"
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

        不要用本工具保存长期记忆、Soul、用户画像、旁注或用户笔记；
        这些内容应写入 Memory 子系统，或在用户明确要求导出文件时才写文件。

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

    def create_scheduled_task(
        self,
        name: str,
        prompt: str,
        schedule_expr: str,
        worker_kind: str = "general",
    ) -> str:
        """创建一个定时任务，系统会在指定时间自动执行，不需要你现在动手做。

        使用示例（每天重复）：
          create_scheduled_task(
              name="每日新闻汇总",
              prompt="搜索今天的热门新闻并生成简洁摘要",
              schedule_expr="0 20 * * *",
              worker_kind="research"
          )

        使用示例（今天下午四点半一次性）：
          create_scheduled_task(
              name="今日热门消息汇总",
              prompt="搜索今天的热门新闻并生成简洁摘要",
              schedule_expr="30 16 13 5 *",
              worker_kind="research"
          )

        Args:
            name: 任务名称，如"每日新闻汇总"。
            prompt: 任务到点后自动执行的完整指令。所有需要在那个时间点做的事都写在这里，
                    系统会拿着这个指令去执行，你不需要现在就去搜索或准备内容。
            schedule_expr: Cron 表达式。"分 时 日 月 周"。
                每天20:00 = "0 20 * * *"
                今天下午16:30 = "30 16 13 5 *"（假设今天是5月13日）
                每周五17:00 = "0 17 * * 5"
            worker_kind: 使用哪种 worker 执行：general、coding、research、solo。

        Returns:
            str: 创建结果，包含任务 ID 和下次执行时间。
        """
        return create_scheduled_task(
            name=name,
            prompt=prompt,
            schedule_expr=schedule_expr,
            worker_kind=worker_kind,
            conversation_id=self.conversation_id,
        )

    def save_memory_note(
        self,
        text: str,
        tags: list[str] | None = None,
        confidence: float = 1.0,
    ) -> str:
        """把一条用户笔记保存到 openEagle 长期记忆。

        当用户说“记住”“记一下”“记下”“记录一下”“以后记得”“加入用户笔记”等时使用。
        这不会在工作区或项目根目录创建 txt/md/json 文件。

        Args:
            text: 要保存的笔记正文。
            tags: 可选标签，如 ["preference"]、["anime"]。
            confidence: 置信度，0 到 1。

        Returns:
            str: 保存结果。
        """
        if self.memory_service is None:
            return "Error: Memory 子系统未初始化，无法保存用户笔记。"
        clean_text = text.strip()
        if not clean_text:
            return "Error: 用户笔记不能为空。"
        clean_tags = [
            str(item).strip()
            for item in (tags or ["user-request"])
            if str(item).strip()
        ]
        note_id = self.memory_service.save_user_note(
            clean_text,
            tags=clean_tags or ["user-request"],
            confidence=confidence,
            source="manual",
        )
        return f"已保存到长期记忆用户笔记: {note_id}"

    def get_memory_state(self, include_archived: bool = False) -> str:
        """读取 openEagle 长期记忆状态，用于查找用户笔记 ID 后再更新或删除。

        Args:
            include_archived: 是否包含已归档/删除的用户笔记。

        Returns:
            str: JSON 格式的用户画像、用户笔记、Soul 和近期审计记录。
        """
        if self.memory_service is None:
            return "Error: Memory 子系统未初始化，无法读取长期记忆。"
        payload = self.memory_service.tool_state_payload(include_archived=include_archived)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def update_memory_note(
        self,
        note_id: str,
        text: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        status: str | None = None,
    ) -> str:
        """更新一条 openEagle 长期记忆用户笔记。

        Args:
            note_id: 要更新的笔记 ID。
            text: 新正文；不传则保留原正文。
            tags: 新标签；不传则保留原标签。
            confidence: 新置信度；不传则保留原值。
            status: active 或 archived；不传则保留原状态。

        Returns:
            str: 更新结果。
        """
        if self.memory_service is None:
            return "Error: Memory 子系统未初始化，无法更新用户笔记。"
        try:
            updated_id = self.memory_service.update_user_note(
                note_id,
                text=text,
                tags=tags,
                confidence=confidence,
                status=status,
                source="manual",
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return f"已更新长期记忆用户笔记: {updated_id}"

    def delete_memory_note(self, note_id: str, reason: str = "") -> str:
        """删除一条 openEagle 长期记忆用户笔记。

        删除会归档笔记并保留审计记录，不会直接抹掉历史。

        Args:
            note_id: 要删除/归档的笔记 ID。
            reason: 可选删除原因。

        Returns:
            str: 删除结果。
        """
        if self.memory_service is None:
            return "Error: Memory 子系统未初始化，无法删除用户笔记。"
        try:
            deleted = self.memory_service.delete_user_note(
                note_id,
                reason=reason,
                source="manual",
            )
        except ValueError as exc:
            return f"Error: {exc}"
        if not deleted:
            return f"Error: 未找到用户笔记: {note_id}"
        return f"已删除长期记忆用户笔记: {note_id}"

    def save_user_profile(self, content: str) -> str:
        """保存完整用户画像到 openEagle 长期记忆。

        Args:
            content: 完整用户画像 markdown 文本。

        Returns:
            str: 保存结果。
        """
        if self.memory_service is None:
            return "Error: Memory 子系统未初始化，无法保存用户画像。"
        self.memory_service.save_profile(content, source="manual")
        return "已保存用户画像到长期记忆。"

    def save_soul_core(self, core: str) -> str:
        """保存 Soul core 到 openEagle 长期记忆。

        仅当用户明确要求修改 Soul 时使用；普通用户偏好应保存为用户笔记或用户画像。

        Args:
            core: 完整 Soul core / SOUL.md 文本。

        Returns:
            str: 保存结果。
        """
        if self.memory_service is None:
            return "Error: Memory 子系统未初始化，无法保存 Soul。"
        self.memory_service.save_soul_core(core, source="manual")
        return "已保存 Soul core 到长期记忆。"

    def save_agent_side_notes(self, side_notes: str) -> str:
        """保存 Agent 旁注到 openEagle 长期记忆。

        用于记录或更新 Agent 对相处方式、称呼、语气的旁注，不要覆盖 Soul core。

        Args:
            side_notes: 完整旁注文本。

        Returns:
            str: 保存结果。
        """
        if self.memory_service is None:
            return "Error: Memory 子系统未初始化，无法保存 Agent 旁注。"
        self.memory_service.save_agent_side_notes(side_notes, source="manual")
        return "已保存 Agent 旁注到长期记忆。"


def build_default_tools(
    workspace_root: Optional[Path] = None,
    confirmation_store: ToolConfirmationStore | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    permission_mode: str = "default",
    builtin_tools: list[dict[str, object]] | None = None,
    web_search_config: WebSearchConfig | None = None,
    attachment_store: AttachmentStore | None = None,
    memory_service: Any | None = None,
    task_context: str | None = None,
) -> OpenEagleDefaultTools:
    root = workspace_root or resolve_workspace_root()
    return OpenEagleDefaultTools(
        workspace_root=root,
        confirmation_store=confirmation_store,
        request_id=request_id,
        conversation_id=conversation_id,
        permission_mode=permission_mode,
        builtin_tools=builtin_tools,
        web_search_config=web_search_config,
        attachment_store=attachment_store,
        memory_service=memory_service,
        task_context=task_context,
    )


def _build_configured_tool_name(tool: ToolConfig) -> str:
    safe_name = re.sub(r"[^0-9A-Za-z_-]+", "_", tool.name).strip("_").lower() or "custom"
    safe_id = re.sub(r"[^0-9A-Za-z]+", "", tool.id).lower() or "tool"
    suffix = safe_id[:8]
    max_name_length = 64 - len("tool__") - len(suffix)
    truncated_name = safe_name[:max_name_length].strip("_") or "custom"
    return f"tool_{truncated_name}_{suffix}"


def build_configured_tools(
    tool_configs: list[ToolConfig],
    workspace_root: Optional[Path] = None,
    confirmation_store: ToolConfirmationStore | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    permission_mode: str = "default",
    task_context: str | None = None,
) -> tuple[list[BaseTool], dict[str, str]]:
    root = (workspace_root or resolve_workspace_root()).resolve()
    tools: list[BaseTool] = []
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
                    contextual_assessment = assess_contextual_tool_action(
                        "configured_tool",
                        params,
                        task_context,
                    )
                    if contextual_assessment is not None:
                        return f"Error: {contextual_assessment.reason}"
                    if assessment.level == "blocked" and not (assessment.overridable and permission_mode == "all"):
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
                    contextual_assessment = assess_contextual_tool_action(
                        "configured_tool",
                        params,
                        task_context,
                    )
                    if contextual_assessment is not None:
                        return f"Error: {contextual_assessment.reason}"
                    if assessment.level == "blocked" and not (assessment.overridable and permission_mode == "all"):
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
            entrypoint.__doc__ = f"执行命令: {tool.command}"

        tools.append(
            StructuredTool.from_function(
                func=entrypoint,
                name=function_name,
                description=" ".join(description_parts),
                args_schema={
                    "type": "object",
                    "properties": {
                        name: {"type": "string", "description": f"填入 {{{name}}} 的字符串参数。"}
                        for name in placeholders
                    },
                    "required": placeholders,
                    "additionalProperties": False,
                } if has_params else None,
                infer_schema=not has_params,
            )
        )
        name_map[function_name] = display_name

    return tools, name_map
