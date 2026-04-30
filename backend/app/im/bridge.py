from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import AppConfig, FeishuConfig
from .commands import parse_im_command
from .feishu import FeishuAdapter
from .models import IMConversationBinding, IMEvent, IMOutboundMessage, IMStatus
from .routing import build_conversation_binding, is_source_allowed

SendClient = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
HandleChat = Callable[[IMConversationBinding, str, str], Awaitable[str]]
StartSolo = Callable[[IMConversationBinding, str, str], Awaitable[str]]
SoloControl = Callable[[str, str, str], Awaitable[str]]
ToolDecision = Callable[[str, str, str], Awaitable[str]]


HELP_TEXT = """openEagle 飞书命令：
/solo <任务> - 启动 SOLO 桌面任务
/pause - 暂停 SOLO
/resume - 恢复 SOLO
/stop - 停止 SOLO
/allow - 允许当前危险动作或工具确认
/reject - 拒绝当前危险动作或工具确认
/help - 查看命令

普通文本会进入 Chat 对话，不会自动启动 SOLO。"""


class IMBridge:
    def __init__(
        self,
        send_client: SendClient,
        handle_chat: HandleChat,
        start_solo: StartSolo,
        solo_control: SoloControl,
        tool_decision: ToolDecision,
    ) -> None:
        self._send_client = send_client
        self._handle_chat = handle_chat
        self._start_solo = start_solo
        self._solo_control = solo_control
        self._tool_decision = tool_decision
        self._feishu_adapter: FeishuAdapter | None = None
        self._feishu_signature: tuple[Any, ...] | None = None
        self._bindings: dict[str, IMConversationBinding] = {}

    async def update_config(self, config: AppConfig) -> None:
        feishu_config = resolve_feishu_config(config)
        signature = _feishu_signature(feishu_config)
        if signature == self._feishu_signature:
            return

        if self._feishu_adapter is not None:
            await self._feishu_adapter.stop()
            self._feishu_adapter = None

        self._feishu_signature = signature
        if not feishu_config.enabled:
            await self._emit_status(IMStatus(provider="feishu", state="disabled", detail="飞书入口未启用。"))
            return

        self._feishu_adapter = FeishuAdapter(
            feishu_config,
            on_event=self._handle_event,
            on_status=self._emit_status,
        )
        await self._feishu_adapter.start()

    async def stop(self) -> None:
        if self._feishu_adapter is not None:
            await self._feishu_adapter.stop()
            self._feishu_adapter = None

    async def send_text(self, conversation_id: str, text: str) -> None:
        adapter = self._adapter_for_conversation(conversation_id)
        binding = self._bindings.get(conversation_id)
        if adapter is None or binding is None or not text.strip():
            return
        await adapter.send_text(IMOutboundMessage(source=binding.source, text=text.strip()))

    async def _handle_event(self, event: IMEvent) -> None:
        config = resolve_feishu_config(await self._current_config())
        binding = build_conversation_binding(event.source)
        self._bindings[binding.conversation_id] = binding

        if not is_source_allowed(config, event.source):
            await self._emit_status(
                IMStatus(
                    provider="feishu",
                    state="connected",
                    detail="已拦截未授权飞书来源。",
                    last_blocked_open_id=event.source.user_id,
                    last_blocked_chat_id=event.source.chat_id,
                )
            )
            await self.send_text(
                binding.conversation_id,
                "openEagle 当前未允许这个飞书来源，请在设置里加入 open_id 或 chat_id。",
            )
            return

        request_id = f"im-{uuid.uuid4()}"
        await self._send_client(
            "server:external_user_message",
            request_id,
            binding.conversation_id,
            {
                "content": event.text,
                "source": "feishu",
                "conversation": _conversation_payload(binding),
            },
        )

        command = parse_im_command(event.text)
        if command.name == "chat":
            reply = await self._handle_chat(binding, command.argument, request_id)
        elif command.name == "solo":
            reply = await self._start_solo(binding, command.argument, request_id)
        elif command.name in {"pause", "resume", "stop"}:
            action = {"pause": "pause", "resume": "resume", "stop": "stop"}[command.name]
            reply = await self._solo_control(binding.conversation_id, request_id, action)
        elif command.name in {"allow", "reject"}:
            decision = "allow" if command.name == "allow" else "reject"
            reply = await self._tool_decision(binding.conversation_id, request_id, decision)
        else:
            reply = HELP_TEXT

        if reply.strip():
            await self.send_text(binding.conversation_id, reply)
            if command.name != "chat":
                await self._send_client(
                    "server:message",
                    request_id,
                    binding.conversation_id,
                    {"content": reply},
                )

    async def _current_config(self) -> AppConfig:
        # Patched by main.py after construction to avoid a circular import.
        raise RuntimeError("IMBridge current config callback is not configured")

    async def _emit_status(self, status: IMStatus) -> None:
        await self._send_client(
            "server:im_status",
            f"im-status-{status.provider}",
            "system",
            {
                "provider": status.provider,
                "state": status.state,
                "detail": status.detail,
                "lastBlockedOpenId": status.last_blocked_open_id,
                "lastBlockedChatId": status.last_blocked_chat_id,
            },
        )

    def _adapter_for_conversation(self, conversation_id: str) -> FeishuAdapter | None:
        binding = self._bindings.get(conversation_id)
        if binding is None or binding.source.channel != "feishu":
            return None
        return self._feishu_adapter


def bind_config_getter(
    bridge: IMBridge,
    getter: Callable[[], AppConfig],
) -> None:
    async def _current_config() -> AppConfig:
        return getter()

    bridge._current_config = _current_config  # type: ignore[method-assign]


def resolve_feishu_config(config: AppConfig) -> FeishuConfig:
    for provider in config.im.providers:
        if provider.type == "feishu":
            return FeishuConfig(
                enabled=provider.enabled,
                appId=provider.app_id,
                appSecret=provider.app_secret,
                allowedOpenIds=provider.allowed_open_ids,
                allowedChatIds=provider.allowed_chat_ids,
            )
    return config.feishu


def _feishu_signature(config: FeishuConfig) -> tuple[Any, ...]:
    return (
        config.enabled,
        config.app_id or "",
        config.app_secret or "",
        tuple(sorted(config.allowed_open_ids)),
        tuple(sorted(config.allowed_chat_ids)),
    )


def _conversation_payload(binding: IMConversationBinding) -> dict[str, str]:
    return {
        "id": binding.conversation_id,
        "title": binding.title,
    }
