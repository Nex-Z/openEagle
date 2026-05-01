from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import AppConfig, FeishuConfig, TelegramConfig
from .commands import parse_im_command
from .feishu import FeishuAdapter
from .models import IMConversationBinding, IMEvent, IMOutboundMessage, IMStatus
from .routing import build_conversation_binding, is_source_allowed
from .telegram import TelegramAdapter

SendClient = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
HandleChat = Callable[[IMConversationBinding, str, str], Awaitable[str]]
StartSolo = Callable[[IMConversationBinding, str, str], Awaitable[str]]
SoloControl = Callable[[str, str, str], Awaitable[str]]
ToolDecision = Callable[[str, str, str], Awaitable[str]]


HELP_TEXT = """openEagle IM 命令：
/chat <内容> - 只进入 Chat 对话，不启动 SOLO
/solo <任务> - 启动 SOLO 桌面任务
/pause - 暂停 SOLO
/resume - 恢复 SOLO
/stop - 停止 SOLO
/allow - 允许当前危险动作或工具确认
/reject - 拒绝当前危险动作或工具确认
/help - 查看命令

普通文本默认按 SOLO 任务处理；想聊天请用 /chat <内容>。"""


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
        self._telegram_adapter: TelegramAdapter | None = None
        self._telegram_signature: tuple[Any, ...] | None = None
        self._bindings: dict[str, IMConversationBinding] = {}

    async def update_config(self, config: AppConfig) -> None:
        await self._update_feishu(config)
        await self._update_telegram(config)

    async def _update_feishu(self, config: AppConfig) -> None:
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

    async def _update_telegram(self, config: AppConfig) -> None:
        telegram_config = resolve_telegram_config(config)
        signature = _telegram_signature(telegram_config)
        if signature == self._telegram_signature:
            return

        if self._telegram_adapter is not None:
            await self._telegram_adapter.stop()
            self._telegram_adapter = None

        self._telegram_signature = signature
        if not telegram_config.enabled:
            await self._emit_status(IMStatus(provider="telegram", state="disabled", detail="Telegram 入口未启用。"))
            return

        self._telegram_adapter = TelegramAdapter(
            telegram_config,
            on_event=self._handle_event,
            on_status=self._emit_status,
        )
        await self._telegram_adapter.start()

    async def stop(self) -> None:
        if self._feishu_adapter is not None:
            await self._feishu_adapter.stop()
            self._feishu_adapter = None
        if self._telegram_adapter is not None:
            await self._telegram_adapter.stop()
            self._telegram_adapter = None

    async def send_text(self, conversation_id: str, text: str) -> None:
        adapter = self._adapter_for_conversation(conversation_id)
        binding = self._bindings.get(conversation_id)
        if adapter is None or binding is None or not text.strip():
            return
        await adapter.send_text(IMOutboundMessage(source=binding.source, text=text.strip()))

    async def _handle_event(self, event: IMEvent) -> None:
        config = resolve_channel_config(await self._current_config(), event.source.channel)
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
                "openEagle 当前未允许这个 IM 来源，请在设置里加入用户 ID 或会话 ID。",
            )
            return

        request_id = f"im-{uuid.uuid4()}"
        await self._send_client(
            "server:external_user_message",
            request_id,
            binding.conversation_id,
            {
                "content": event.text,
                "source": event.source.channel,
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
            if command.name not in {"chat", "solo"}:
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

    def _adapter_for_conversation(
        self,
        conversation_id: str,
    ) -> FeishuAdapter | TelegramAdapter | None:
        binding = self._bindings.get(conversation_id)
        if binding is None:
            return None
        if binding.source.channel == "feishu":
            return self._feishu_adapter
        if binding.source.channel == "telegram":
            return self._telegram_adapter
        return None


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


def resolve_telegram_config(config: AppConfig) -> TelegramConfig:
    for provider in config.im.providers:
        if provider.type == "telegram":
            return TelegramConfig(
                enabled=provider.enabled,
                botToken=provider.bot_token,
                allowedUserIds=provider.allowed_user_ids,
                allowedChatIds=provider.allowed_chat_ids,
            )
    return config.telegram


def resolve_channel_config(
    config: AppConfig,
    channel: str,
) -> FeishuConfig | TelegramConfig:
    if channel == "telegram":
        return resolve_telegram_config(config)
    return resolve_feishu_config(config)


def _feishu_signature(config: FeishuConfig) -> tuple[Any, ...]:
    return (
        config.enabled,
        config.app_id or "",
        config.app_secret or "",
        tuple(sorted(config.allowed_open_ids)),
        tuple(sorted(config.allowed_chat_ids)),
    )


def _telegram_signature(config: TelegramConfig) -> tuple[Any, ...]:
    return (
        config.enabled,
        config.bot_token or "",
        tuple(sorted(config.allowed_user_ids)),
        tuple(sorted(config.allowed_chat_ids)),
    )


def _conversation_payload(binding: IMConversationBinding) -> dict[str, str]:
    return {
        "id": binding.conversation_id,
        "title": binding.title,
    }
