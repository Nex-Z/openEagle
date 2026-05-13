from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..attachments import AttachmentStore, append_attachment_context
from ..config import AppConfig, FeishuConfig, TelegramConfig, WechatConfig
from ..models import AttachmentRef
from .commands import parse_im_command
from .feishu import FeishuAdapter
from .models import IMConversationBinding, IMEvent, IMOutboundMessage, IMStatus
from .routing import build_conversation_binding, is_source_allowed
from .telegram import TelegramAdapter
from .wechat import WechatAdapter

SendClient = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]
HandleChat = Callable[
    [IMConversationBinding, str, str, list[AttachmentRef] | None],
    Awaitable[str],
]
StartSolo = Callable[[IMConversationBinding, str, str], Awaitable[str]]
SoloControl = Callable[[str, str, str], Awaitable[str]]
ToolDecision = Callable[[str, str, str], Awaitable[str]]
ReplyAttachments = Callable[[str, str], list[AttachmentRef]]

HELP_TEXT = """openEagle IM 命令：
/solo <任务> - 明确让 main agent 优先调度桌面执行
/pause - 暂停桌面执行
/resume - 恢复桌面执行
/stop - 停止桌面执行
/allow - 允许当前危险动作或工具确认
/reject - 拒绝当前危险动作或工具确认
/help - 查看命令

普通文本会先交给 main agent 理解，再由 main agent 决定直接回复或调度 worker。"""


class IMBridge:
    def __init__(
        self,
        send_client: SendClient,
        handle_chat: HandleChat,
        start_solo: StartSolo,
        solo_control: SoloControl,
        tool_decision: ToolDecision,
        attachment_store: AttachmentStore | None = None,
        reply_attachments: ReplyAttachments | None = None,
    ) -> None:
        self._send_client = send_client
        self._handle_chat = handle_chat
        self._start_solo = start_solo
        self._solo_control = solo_control
        self._tool_decision = tool_decision
        self._attachment_store = attachment_store
        self._reply_attachments = reply_attachments
        self._feishu_adapter: FeishuAdapter | None = None
        self._feishu_signature: tuple[Any, ...] | None = None
        self._telegram_adapter: TelegramAdapter | None = None
        self._telegram_signature: tuple[Any, ...] | None = None
        self._wechat_adapter: WechatAdapter | None = None
        self._wechat_signature: tuple[Any, ...] | None = None
        self._bindings: dict[str, IMConversationBinding] = {}

    async def update_config(self, config: AppConfig) -> None:
        await self._update_feishu(config)
        await self._update_telegram(config)
        await self._update_wechat(config)

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

    async def _update_wechat(self, config: AppConfig) -> None:
        wechat_config = resolve_wechat_config(config)
        signature = _wechat_signature(wechat_config)
        if signature == self._wechat_signature:
            return

        if self._wechat_adapter is not None:
            await self._wechat_adapter.stop()
            self._wechat_adapter = None

        self._wechat_signature = signature
        if not wechat_config.enabled:
            await self._emit_status(IMStatus(provider="wechat", state="disabled", detail="微信 ClawBot 入口未启用。"))
            return

        self._wechat_adapter = WechatAdapter(
            wechat_config,
            on_event=self._handle_event,
            on_status=self._emit_status,
        )
        await self._wechat_adapter.start()

    async def stop(self) -> None:
        if self._feishu_adapter is not None:
            await self._feishu_adapter.stop()
            self._feishu_adapter = None
        if self._telegram_adapter is not None:
            await self._telegram_adapter.stop()
            self._telegram_adapter = None
        if self._wechat_adapter is not None:
            await self._wechat_adapter.stop()
            self._wechat_adapter = None

    async def start_wechat_bind(
        self,
        request_id: str,
        conversation_id: str,
        force: bool = False,
    ) -> None:
        adapter = await self._ensure_wechat_adapter()
        await adapter.start_bind(
            lambda payload: self._emit_wechat_bind_status(request_id, conversation_id, payload),
            force=force,
        )

    async def cancel_wechat_bind(
        self,
        request_id: str,
        conversation_id: str,
    ) -> None:
        adapter = await self._ensure_wechat_adapter()
        await adapter.cancel_bind(
            lambda payload: self._emit_wechat_bind_status(request_id, conversation_id, payload)
        )

    async def unbind_wechat(
        self,
        request_id: str,
        conversation_id: str,
    ) -> None:
        adapter = await self._ensure_wechat_adapter()
        await adapter.unbind(
            lambda payload: self._emit_wechat_bind_status(request_id, conversation_id, payload)
        )

    async def send_text(
        self,
        conversation_id: str,
        text: str,
        attachments: list[AttachmentRef] | None = None,
    ) -> None:
        adapter = self._adapter_for_conversation(conversation_id)
        binding = self._bindings.get(conversation_id)
        if adapter is None or binding is None:
            return
        clean_text = text.strip()
        clean_attachments = attachments or []
        if not clean_text and not clean_attachments:
            return
        await adapter.send_text(
            IMOutboundMessage(
                source=binding.source,
                text=clean_text,
                attachments=clean_attachments,
            )
        )

    async def _handle_event(self, event: IMEvent) -> None:
        config = resolve_channel_config(await self._current_config(), event.source.channel)
        binding = build_conversation_binding(event.source)
        self._bindings[binding.conversation_id] = binding

        if not is_source_allowed(config, event.source):
            await self._emit_status(
                IMStatus(
                    provider=event.source.channel,
                    state="connected",
                    detail=f"已拦截未授权{_provider_label(event.source.channel)}来源。",
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
        command = parse_im_command(event.text, allow_empty_task=bool(event.attachments))
        explicit_command = event.text.strip().startswith("/")
        attachments = await self._prepare_event_attachments(event, binding)
        await self._send_client(
            "server:external_user_message",
            request_id,
            binding.conversation_id,
            {
                "content": event.text,
                "attachments": self._attachment_store.public_dicts(attachments)
                if self._attachment_store is not None
                else [item.model_dump(by_alias=True, exclude_none=True) for item in attachments],
                "source": event.source.channel,
                "conversation": _conversation_payload(binding),
            },
        )

        attachment_errors = [item for item in attachments if item.status == "error"]
        if attachment_errors:
            reply = _attachment_error_reply(attachment_errors)
        elif attachments and not explicit_command:
            reply = await self._handle_chat(
                binding,
                event.text.strip() or "请处理这些附件。",
                request_id,
                attachments,
            )
        elif command.name == "auto":
            reply = await self._handle_chat(
                binding,
                command.argument,
                request_id,
                attachments,
            )
        elif command.name == "solo":
            task_text = command.argument or "请结合附件执行任务。"
            task = append_attachment_context(task_text, attachments) if attachments else task_text
            reply = await self._start_solo(binding, task, request_id)
        elif command.name in {"pause", "resume", "stop"}:
            action = {"pause": "pause", "resume": "resume", "stop": "stop"}[command.name]
            reply = await self._solo_control(binding.conversation_id, request_id, action)
        elif command.name in {"allow", "reject"}:
            decision = "allow" if command.name == "allow" else "reject"
            reply = await self._tool_decision(binding.conversation_id, request_id, decision)
        else:
            reply = HELP_TEXT

        emit_client_reply = command.name not in {"auto", "solo"} or bool(attachment_errors)
        reply_attachments = (
            self._reply_attachments(binding.conversation_id, request_id)
            if self._reply_attachments is not None
            else []
        )
        if reply.strip() or reply_attachments:
            await self.send_text(binding.conversation_id, reply, reply_attachments)
            if emit_client_reply:
                await self._send_client(
                    "server:message",
                    request_id,
                    binding.conversation_id,
                    {
                        "content": reply,
                        "attachments": self._attachment_store.public_dicts(reply_attachments)
                        if self._attachment_store is not None
                        else [item.model_dump(by_alias=True, exclude_none=True) for item in reply_attachments],
                    },
                )

    async def _current_config(self) -> AppConfig:
        # Patched by main.py after construction to avoid a circular import.
        raise RuntimeError("IMBridge current config callback is not configured")

    async def _prepare_event_attachments(
        self,
        event: IMEvent,
        binding: IMConversationBinding,
    ) -> list[AttachmentRef]:
        if not event.attachments:
            return []
        if self._attachment_store is None:
            return [
                item.model_copy(update={"status": "error", "error": "附件仓库未初始化。"})
                for item in event.attachments
            ]
        adapter = self._adapter_for_conversation(binding.conversation_id)
        downloader = getattr(adapter, "download_event_attachments", None)
        if downloader is None:
            return [
                item.model_copy(update={"status": "error", "error": "当前 IM 入口不支持下载附件。"})
                for item in event.attachments
            ]
        try:
            return await downloader(event, self._attachment_store, binding.conversation_id)
        except Exception as exc:  # noqa: BLE001
            return [
                item.model_copy(update={"status": "error", "error": f"附件下载失败: {exc}"})
                for item in event.attachments
            ]

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
    ) -> FeishuAdapter | TelegramAdapter | WechatAdapter | None:
        binding = self._bindings.get(conversation_id)
        if binding is None:
            return None
        if binding.source.channel == "feishu":
            return self._feishu_adapter
        if binding.source.channel == "telegram":
            return self._telegram_adapter
        if binding.source.channel == "wechat":
            return self._wechat_adapter
        return None

    async def _ensure_wechat_adapter(self) -> WechatAdapter:
        if self._wechat_adapter is not None:
            return self._wechat_adapter
        config = resolve_wechat_config(await self._current_config())
        self._wechat_adapter = WechatAdapter(
            config,
            on_event=self._handle_event,
            on_status=self._emit_status,
        )
        return self._wechat_adapter

    async def _emit_wechat_bind_status(
        self,
        request_id: str,
        conversation_id: str,
        payload: dict[str, Any],
    ) -> None:
        await self._send_client(
            "server:wechat_bind_status",
            request_id,
            conversation_id,
            payload,
        )


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


def resolve_wechat_config(config: AppConfig) -> WechatConfig:
    for provider in config.im.providers:
        if provider.type == "wechat":
            return WechatConfig(
                enabled=provider.enabled,
                accountId=provider.account_id,
                baseUrl=provider.base_url,
                botType=provider.bot_type,
                allowedUserIds=provider.allowed_user_ids,
                allowedChatIds=provider.allowed_chat_ids,
            )
    return config.wechat


def resolve_channel_config(
    config: AppConfig,
    channel: str,
) -> FeishuConfig | TelegramConfig | WechatConfig:
    if channel == "telegram":
        return resolve_telegram_config(config)
    if channel == "wechat":
        return resolve_wechat_config(config)
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


def _wechat_signature(config: WechatConfig) -> tuple[Any, ...]:
    return (
        config.enabled,
        config.account_id or "",
        config.base_url or "",
        config.bot_type or "",
        tuple(sorted(config.allowed_user_ids)),
        tuple(sorted(config.allowed_chat_ids)),
    )


def _conversation_payload(binding: IMConversationBinding) -> dict[str, str]:
    return {
        "id": binding.conversation_id,
        "title": binding.title,
    }


def _attachment_error_reply(attachments: list[AttachmentRef]) -> str:
    rows = [
        f"- {item.name or item.id}: {item.error or '附件处理失败。'}"
        for item in attachments
    ]
    return "附件处理失败，未调用模型。\n" + "\n".join(rows)


def _provider_label(channel: str) -> str:
    return {
        "feishu": "飞书",
        "telegram": "Telegram",
        "wechat": "微信",
    }.get(channel, "IM")
