from __future__ import annotations

import inspect
import asyncio
import json
import os
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.tools import BaseTool, StructuredTool

from .config import AppConfig, McpConfig, SkillConfig, ToolConfig
from .confirmations import PendingToolConfirmation
from .default_tools import (
    _build_configured_tool_name,
    build_default_tools,
    execute_confirmed_tool,
)
from .memory import MemoryService
from .safety import RiskAssessment, assess_tool_action

SOLO_CONFIRMATION_PREFIX = "SOLO_CONFIRMATION_REQUIRED "


@dataclass
class SoloCapabilityTrace:
    kind: str
    name: str
    status: str
    summary: str
    params: dict[str, Any] = field(default_factory=dict)
    result: Any | None = None


@dataclass
class SoloConfirmationRequest:
    action: str
    action_args: dict[str, Any]
    reason: str
    name: str
    kind: str


@dataclass
class McpConnection:
    session: Any
    stack: AsyncExitStack

    async def aclose(self) -> None:
        await self.stack.aclose()


def _tool_from_callable(fn: Any, *, name: str | None = None, description: str | None = None) -> BaseTool:
    return StructuredTool.from_function(
        func=fn,
        name=name or fn.__name__,
        description=description or inspect.getdoc(fn) or fn.__name__,
    )


def _split_stdio_endpoint(endpoint: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(endpoint)

    import ctypes
    from ctypes import wintypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = command_line_to_argv(endpoint, ctypes.byref(argc))
    if not argv:
        raise ValueError("无法解析 MCP stdio endpoint。")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


class SoloDefaultCapabilities:
    def __init__(self, default_tools: Any, web_search_enabled: bool = True) -> None:
        self._default_tools = default_tools
        tools = [
            self.get_current_time,
            self.get_file_info,
            self.list_directory,
            self.read_text_file,
            self.search_files,
            self.search_text,
            self.get_memory_state,
            self.save_memory_note,
            self.update_memory_note,
            self.delete_memory_note,
            self.save_user_profile,
            self.save_soul_core,
            self.save_agent_side_notes,
        ]
        if web_search_enabled:
            tools.append(self.web_search)
        self.name = "solo_default_capabilities"
        self.instructions = (
            "桌面执行 worker 可主动使用这些只读默认工具收集信息、读取工作区文本、搜索文件和联网查询。"
            "记忆类请求使用 get_memory_state/save_memory_note/update_memory_note/delete_memory_note 等工具读写 Memory，"
            "不要在项目根目录创建记忆文件。"
        )
        self._agent_tools = [_tool_from_callable(tool) for tool in tools]

    @property
    def agent_tools(self) -> list[BaseTool]:
        return list(self._agent_tools)

    def get_current_time(self) -> str:
        """获取当前系统日期时间。"""
        return self._default_tools.get_current_time()

    def get_file_info(self, path: str) -> str:
        """获取工作区内文件或目录的信息。"""
        return self._default_tools.get_file_info(path)

    def list_directory(self, path: str = ".") -> str:
        """列出工作区内目录内容。"""
        return self._default_tools.list_directory(path)

    def read_text_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_chars: int = 8000,
        include_line_numbers: bool = False,
    ) -> str:
        """读取工作区内文本文件内容。"""
        return self._default_tools.read_text_file(
            path,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
            include_line_numbers=include_line_numbers,
        )

    def search_files(self, keyword: str, path: str = ".", max_results: int = 50) -> str:
        """按文件名搜索工作区文件。"""
        return self._default_tools.search_files(keyword, path=path, max_results=max_results)

    def search_text(self, keyword: str, path: str = ".", max_results: int = 50) -> str:
        """按文本内容搜索工作区文件。"""
        return self._default_tools.search_text(keyword, path=path, max_results=max_results)

    def web_search(self, query: str, max_results: int = 5) -> str:
        """联网搜索资料。"""
        return self._default_tools.web_search(query, max_results=max_results)

    def save_memory_note(
        self,
        text: str,
        tags: list[str] | None = None,
        confidence: float = 1.0,
    ) -> str:
        """保存用户笔记到长期记忆，不创建项目文件。"""
        return self._default_tools.save_memory_note(text, tags=tags, confidence=confidence)

    def get_memory_state(self, include_archived: bool = False) -> str:
        """读取长期记忆状态，用于查找用户笔记 ID。"""
        return self._default_tools.get_memory_state(include_archived=include_archived)

    def update_memory_note(
        self,
        note_id: str,
        text: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        status: str | None = None,
    ) -> str:
        """更新长期记忆用户笔记。"""
        return self._default_tools.update_memory_note(
            note_id,
            text=text,
            tags=tags,
            confidence=confidence,
            status=status,
        )

    def delete_memory_note(self, note_id: str, reason: str = "") -> str:
        """删除/归档长期记忆用户笔记。"""
        return self._default_tools.delete_memory_note(note_id, reason=reason)

    def save_user_profile(self, content: str) -> str:
        """保存完整用户画像到长期记忆。"""
        return self._default_tools.save_user_profile(content)

    def save_soul_core(self, core: str) -> str:
        """保存 Soul core 到长期记忆。"""
        return self._default_tools.save_soul_core(core)

    def save_agent_side_notes(self, side_notes: str) -> str:
        """保存 Agent 自动旁注到长期记忆。"""
        return self._default_tools.save_agent_side_notes(side_notes)


class SoloCapabilityRuntime:
    def __init__(
        self,
        config: AppConfig,
        workspace_root: Path,
        request_id: str,
        conversation_id: str,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self.workspace_root = workspace_root.resolve()
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.memory_service = memory_service
        self.permission_mode = self.config.permissions.mode
        self.default_tools = build_default_tools(
            workspace_root=self.workspace_root,
            request_id=request_id,
            conversation_id=conversation_id,
            permission_mode=self.permission_mode,
            builtin_tools=[bt.model_dump() for bt in self.config.builtin_tools],
            memory_service=self.memory_service,
        )
        enabled_builtins = {
            bt.id: bt.enabled
            for bt in self.config.builtin_tools
        }
        self.default_capabilities = SoloDefaultCapabilities(
            self.default_tools,
            web_search_enabled=enabled_builtins.get("web_search", True),
        )
        self._configured_by_id = {
            tool.id: tool
            for tool in self.config.tools
            if tool.enabled and tool.command.strip()
        }
        self._configured_agent_tool_to_tool_id: dict[str, str] = {}
        self._mcp_toolkits: dict[str, Any] = {}
        self._mcp_tools: dict[tuple[str, str], BaseTool] = {}
        self._mcp_agent_tool_to_call: dict[str, tuple[str, str]] = {}
        self._agent_tools: list[BaseTool] = []
        self._catalog_lines: list[str] = []
        self._confirmation_token = uuid4().hex

    @property
    def agent_tools(self) -> list[BaseTool]:
        return [*self.default_capabilities.agent_tools, *self._agent_tools]

    @property
    def has_agent_tools(self) -> bool:
        return bool(self.agent_tools)

    @property
    def confirmation_token(self) -> str:
        return self._confirmation_token

    def skill_instructions(self) -> list[str]:
        instructions = []
        for skill in self.enabled_skills:
            body = skill.prompt.strip()
            if body:
                instructions.append(
                    f"启用 Skill「{skill.name}」：{skill.description or '无说明'}\n{body}"
                )
        return instructions

    @property
    def enabled_skills(self) -> list[SkillConfig]:
        return [skill for skill in self.config.skills if skill.enabled and skill.prompt.strip()]

    async def initialize(self) -> list[SoloCapabilityTrace]:
        traces: list[SoloCapabilityTrace] = []
        self._build_configured_tools()
        traces.extend(await self._connect_mcp_servers())
        self._build_catalog()
        for skill in self.enabled_skills:
            traces.append(
                SoloCapabilityTrace(
                    kind="skill",
                    name=skill.name,
                    status="completed",
                    summary="桌面执行已自动加载该 Skill 提示。",
                    params={"description": skill.description, "prompt": skill.prompt},
                )
            )
        return traces

    async def close(self) -> None:
        for toolkit in list(self._mcp_toolkits.values()):
            await self._close_mcp_toolkit(toolkit)
        self._mcp_toolkits.clear()

    async def _close_mcp_toolkit(self, toolkit: Any) -> None:
        close = getattr(toolkit, "close", None)
        if close is not None:
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass

        aclose = getattr(toolkit, "aclose", None)
        if aclose is not None:
            try:
                result = aclose()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
            return

        # Failed MCP connects can still have entered these contexts.
        for attr in ("_session_context", "_context"):
            context = getattr(toolkit, attr, None)
            if context is None:
                continue
            exit_method = getattr(context, "__aexit__", None)
            if exit_method is None:
                continue
            try:
                result = exit_method(None, None, None)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
            finally:
                try:
                    setattr(toolkit, attr, None)
                except Exception:
                    pass
        for attr, value in (("session", None), ("_initialized", False)):
            try:
                setattr(toolkit, attr, value)
            except Exception:
                pass

    def capability_catalog(self) -> str:
        return "\n".join(self._catalog_lines)

    def assess_action(self, action: str, action_args: dict[str, Any]) -> RiskAssessment | None:
        if action == "run_configured_tool":
            try:
                params = self._configured_tool_params(action_args)
            except ValueError as exc:
                return RiskAssessment("blocked", str(exc))
            return assess_tool_action("configured_tool", params, self.workspace_root)
        if action == "call_mcp_tool":
            try:
                self._mcp_lookup(action_args)
            except ValueError as exc:
                return RiskAssessment("blocked", str(exc))
            return RiskAssessment("confirm", "MCP 工具可能访问外部资源或修改状态，需要确认。")
        return None

    def execute_action(self, action: str, action_args: dict[str, Any]) -> dict[str, Any]:
        if action == "run_configured_tool":
            output = self._execute_configured_tool(action_args)
            return {"ok": True, "action": action, "output": output}
        raise ValueError(f"unsupported sync capability action: {action}")

    async def execute_action_async(self, action: str, action_args: dict[str, Any]) -> dict[str, Any]:
        if action == "call_mcp_tool":
            output = await self._execute_mcp_tool(action_args)
            return {"ok": True, "action": action, "output": output}
        return await asyncio.to_thread(self.execute_action, action, action_args)

    def _build_configured_tools(self) -> None:
        for tool in self._configured_by_id.values():
            agent_tool_name = _build_configured_tool_name(tool)
            self._configured_agent_tool_to_tool_id[agent_tool_name] = tool.id
            placeholders = list(dict.fromkeys(re.findall(r"\{(\w+)\}", tool.command)))
            properties = {
                name: {
                    "type": "string",
                    "description": f"填入自定义工具「{tool.name}」命令模板中的 {{{name}}}。",
                }
                for name in placeholders
            }
            parameters = {
                "type": "object",
                "properties": properties,
                "required": placeholders,
                "additionalProperties": False,
            }
            description = (
                f"自定义工具「{tool.name}」。{tool.description or '无说明'} "
                f"命令模板: {tool.command}。cwd: {tool.cwd or '.'}。"
            )

            def entrypoint(_tool_id: str = tool.id, **kwargs: str) -> str:
                return self._execute_configured_tool_from_agent(_tool_id, kwargs)

            self._agent_tools.append(
                StructuredTool.from_function(
                    func=entrypoint,
                    name=agent_tool_name,
                    description=description,
                    args_schema=parameters,
                    infer_schema=False,
                )
            )

    async def _connect_mcp_servers(self) -> list[SoloCapabilityTrace]:
        traces: list[SoloCapabilityTrace] = []
        enabled = [server for server in self.config.mcp if server.enabled and server.endpoint.strip()]
        if not enabled:
            return traces

        for server in enabled:
            connection: McpConnection | None = None
            try:
                transport = server.transport or "stdio"
                connection = await self._open_mcp_connection(server, transport)
                tools_result = await connection.session.list_tools()
                tools = list(getattr(tools_result, "tools", []) or [])
                self._register_mcp_tools(server, connection, tools)
                self._mcp_toolkits[server.id] = connection
                traces.append(
                    SoloCapabilityTrace(
                        kind="mcp",
                        name=server.name,
                        status="completed",
                        summary="桌面执行已连接并加载 MCP 工具。",
                        params={"transport": transport, "endpoint": server.endpoint},
                        result={"toolCount": len(tools)},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                if connection is not None:
                    await self._close_mcp_toolkit(connection)
                    self._mcp_toolkits.pop(server.id, None)
                traces.append(
                    SoloCapabilityTrace(
                        kind="mcp",
                        name=server.name,
                        status="error",
                        summary="桌面执行连接 MCP 失败，已跳过该 server。",
                        params={"transport": server.transport, "endpoint": server.endpoint},
                        result=str(exc),
                    )
                )
        return traces

    async def _open_mcp_connection(self, server: McpConfig, transport: str) -> McpConnection:
        from mcp import ClientSession

        stack = AsyncExitStack()
        try:
            if transport == "stdio":
                from mcp.client.stdio import StdioServerParameters, stdio_client

                parts = _split_stdio_endpoint(server.endpoint)
                if not parts:
                    raise ValueError("MCP stdio endpoint 不能为空。")
                params = StdioServerParameters(command=parts[0], args=parts[1:])
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            elif transport == "sse":
                from mcp.client.sse import sse_client

                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(server.endpoint)
                )
            elif transport in {"http", "streamable-http"}:
                from mcp.client.streamable_http import streamablehttp_client

                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamablehttp_client(server.endpoint)
                )
            else:
                raise ValueError(f"不支持的 MCP transport: {transport}")

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            return McpConnection(session=session, stack=stack)
        except Exception:
            await stack.aclose()
            raise

    def _register_mcp_tools(
        self,
        server: McpConfig,
        connection: McpConnection,
        tools: list[Any],
    ) -> None:
        for tool in tools:
            raw_name = str(getattr(tool, "name", "") or "").strip()
            if not raw_name:
                continue
            description = str(getattr(tool, "description", "") or server.description or "")
            parameters = getattr(tool, "inputSchema", None)
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}, "required": []}
            safe_server = re.sub(r"[^0-9A-Za-z]+", "_", server.name).strip("_").lower() or "mcp"
            safe_tool = re.sub(r"[^0-9A-Za-z_]+", "_", raw_name).strip("_").lower() or "tool"
            agent_tool_name = f"mcp_{safe_server}_{safe_tool}"[:64].strip("_")
            if agent_tool_name in self._mcp_agent_tool_to_call:
                agent_tool_name = f"{agent_tool_name[:55]}_{server.id[:8]}"

            async def raw_entrypoint(
                _connection: McpConnection = connection,
                _tool_name: str = raw_name,
                **kwargs: Any,
            ) -> str:
                result = await _connection.session.call_tool(_tool_name, kwargs)
                return stringify_tool_result(result)

            self._mcp_tools[(server.id, raw_name)] = StructuredTool.from_function(
                coroutine=raw_entrypoint,
                name=raw_name,
                description=description or raw_name,
                args_schema=parameters,
                infer_schema=False,
            )
            self._mcp_agent_tool_to_call[agent_tool_name] = (server.id, raw_name)

            async def entrypoint(
                _server_id: str = server.id,
                _tool_name: str = raw_name,
                **kwargs: Any,
            ) -> str:
                return await self._execute_mcp_tool_from_agent(_server_id, _tool_name, kwargs)

            self._agent_tools.append(
                StructuredTool.from_function(
                    coroutine=entrypoint,
                    name=agent_tool_name,
                    description=(
                        f"MCP 工具「{raw_name}」，来自 server「{server.name}」。"
                        f"{description}"
                    ),
                    args_schema=parameters,
                    infer_schema=False,
                )
            )

    def _build_catalog(self) -> None:
        lines = [
            "默认工具: get_current_time, get_file_info, list_directory, read_text_file, search_files, search_text, web_search(若启用)，以及 get_memory_state/save_memory_note/update_memory_note/delete_memory_note/save_user_profile/save_soul_core/save_agent_side_notes。"
        ]
        if self._configured_by_id:
            lines.append("自定义工具（可主动调用；默认权限下按命令风险确认）：")
            for agent_tool_name, tool_id in self._configured_agent_tool_to_tool_id.items():
                tool = self._configured_by_id[tool_id]
                lines.append(
                    f"- {agent_tool_name}: id={tool.id}, name={tool.name}, cwd={tool.cwd or '.'}, "
                    f"description={tool.description or '无'}, command={tool.command}"
                )
        if self._mcp_agent_tool_to_call:
            lines.append("MCP 工具（可主动调用；default 权限下调用前确认）：")
            server_by_id = {server.id: server for server in self.config.mcp}
            for agent_tool_name, (server_id, raw_name) in self._mcp_agent_tool_to_call.items():
                server = server_by_id.get(server_id)
                server_name = server.name if server else server_id
                lines.append(f"- {agent_tool_name}: server_id={server_id}, server={server_name}, tool={raw_name}")
        if self.enabled_skills:
            lines.append("启用的 Skills（必须自动遵循，不需要用户点名）：")
            for skill in self.enabled_skills:
                lines.append(f"- {skill.name}: {skill.description or '无说明'}")
        self._catalog_lines = lines

    def _configured_tool_params(self, action_args: dict[str, Any]) -> dict[str, Any]:
        tool_id = str(action_args.get("tool_id") or action_args.get("toolId") or "").strip()
        if not tool_id:
            name = str(action_args.get("tool_name") or action_args.get("name") or "").strip()
            matches = [tool for tool in self._configured_by_id.values() if tool.name == name]
            if matches:
                tool_id = matches[0].id
        tool = self._configured_by_id.get(tool_id)
        if tool is None:
            raise ValueError("未找到已启用的自定义工具。")
        arguments = action_args.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {
                key: value
                for key, value in action_args.items()
                if key not in {"tool_id", "toolId", "tool_name", "name"}
            }
        command = tool.command
        for placeholder in list(dict.fromkeys(re.findall(r"\{(\w+)\}", tool.command))):
            value = str(arguments.get(placeholder, ""))
            if not value:
                raise ValueError(f"自定义工具「{tool.name}」缺少参数 {placeholder}。")
            command = command.replace(f"{{{placeholder}}}", value)
        return {
            "toolId": tool.id,
            "toolName": tool.name,
            "command": command,
            "cwd": tool.cwd.strip() or ".",
            "timeout_ms": int(tool.timeout_ms),
            "tail": int(tool.tail),
        }

    def _execute_configured_tool_from_agent(self, tool_id: str, arguments: dict[str, Any]) -> str:
        action_args = {"tool_id": tool_id, "arguments": arguments}
        assessment = self.assess_action("run_configured_tool", action_args)
        if assessment is None:
            return "Error: 无法评估自定义工具风险。"
        if assessment.level == "blocked" and not (assessment.overridable and self.permission_mode == "all"):
            return f"Error: {assessment.reason}"
        if assessment.level == "confirm" and self.permission_mode != "all":
            return self._confirmation_payload(
                action="run_configured_tool",
                action_args=action_args,
                reason=assessment.reason,
                kind="tool",
                name=self._configured_by_id.get(tool_id).name if tool_id in self._configured_by_id else tool_id,
            )
        return self._execute_configured_tool(action_args)

    def _execute_configured_tool(self, action_args: dict[str, Any]) -> str:
        params = self._configured_tool_params(action_args)
        return execute_confirmed_tool(
            self.workspace_root,
            PendingToolConfirmation(
                confirmation_id="solo-auto",
                request_id=self.request_id,
                conversation_id=self.conversation_id,
                kind="tool",
                name=str(params.get("toolName") or "configured_tool"),
                reason="桌面执行能力调用",
                params=params,
            ),
        )

    def _mcp_lookup(self, action_args: dict[str, Any]) -> tuple[str, str, BaseTool]:
        server_id = str(action_args.get("server_id") or action_args.get("serverId") or "").strip()
        tool_name = str(action_args.get("tool_name") or action_args.get("toolName") or "").strip()
        if not server_id or not tool_name:
            raise ValueError("MCP 调用缺少 server_id 或 tool_name。")
        tool = self._mcp_tools.get((server_id, tool_name))
        if tool is None:
            raise ValueError("未找到已加载的 MCP 工具。")
        return server_id, tool_name, tool

    async def _execute_mcp_tool_from_agent(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        action_args = {"server_id": server_id, "tool_name": tool_name, "arguments": arguments}
        assessment = self.assess_action("call_mcp_tool", action_args)
        if assessment is None:
            return "Error: 无法评估 MCP 工具风险。"
        if assessment.level == "blocked" and not (assessment.overridable and self.permission_mode == "all"):
            return f"Error: {assessment.reason}"
        if assessment.level == "confirm" and self.permission_mode != "all":
            return self._confirmation_payload(
                action="call_mcp_tool",
                action_args=action_args,
                reason=assessment.reason,
                kind="mcp",
                name=f"{server_id}/{tool_name}",
            )
        return await self._execute_mcp_tool(action_args)

    async def _execute_mcp_tool(self, action_args: dict[str, Any]) -> str:
        _, _, tool = self._mcp_lookup(action_args)
        arguments = action_args.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        result = await tool.ainvoke(arguments)
        return stringify_tool_result(result)

    def _confirmation_payload(
        self,
        action: str,
        action_args: dict[str, Any],
        reason: str,
        kind: str,
        name: str,
    ) -> str:
        payload = {
            "action": action,
            "action_args": action_args,
            "reason": reason,
            "kind": kind,
            "name": name,
            "token": self._confirmation_token,
        }
        return SOLO_CONFIRMATION_PREFIX + json.dumps(payload, ensure_ascii=False)


def stringify_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
            else:
                parts.append(str(item))
        if parts:
            return "\n".join(parts)
    structured = getattr(value, "structuredContent", None)
    if structured is not None:
        return stringify_tool_result(structured)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def parse_confirmation_request(
    value: Any,
    expected_token: str | None = None,
) -> SoloConfirmationRequest | None:
    text = stringify_tool_result(value).strip()
    if not text.startswith(SOLO_CONFIRMATION_PREFIX):
        return None
    payload_text = text[len(SOLO_CONFIRMATION_PREFIX) :].strip()
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if expected_token is not None and payload.get("token") != expected_token:
        return None
    action_args = payload.get("action_args")
    if not isinstance(action_args, dict):
        action_args = {}
    return SoloConfirmationRequest(
        action=str(payload.get("action") or ""),
        action_args=action_args,
        reason=str(payload.get("reason") or "该能力需要用户确认。"),
        name=str(payload.get("name") or payload.get("action") or "capability"),
        kind=str(payload.get("kind") or "tool"),
    )
