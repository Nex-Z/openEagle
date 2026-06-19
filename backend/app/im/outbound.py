from __future__ import annotations

from collections.abc import Callable

from ..config import AppConfig
from .bridge import resolve_channel_config
from .feishu import FeishuAdapter
from .models import IMEvent, IMMessageSource, IMOutboundMessage, IMStatus
from .telegram import TelegramAdapter
from .wechat import WechatAdapter

ConfigGetter = Callable[[], AppConfig]


async def _ignore_event(_event: IMEvent) -> None:
    return None


async def _ignore_status(_status: IMStatus) -> None:
    return None


class RemoteOutboundService:
    def __init__(self, config_getter: ConfigGetter) -> None:
        self._config_getter = config_getter

    async def send_text(self, channel: str, chat_id: str, text: str) -> None:
        config = resolve_channel_config(self._config_getter(), channel)
        if not config.enabled:
            raise RuntimeError(f"远程渠道尚未启用: {channel}")
        if channel == "feishu":
            adapter = FeishuAdapter(config, _ignore_event, _ignore_status)
        elif channel == "telegram":
            adapter = TelegramAdapter(config, _ignore_event, _ignore_status)
        elif channel == "wechat":
            adapter = WechatAdapter(config, _ignore_event, _ignore_status)
        else:
            raise RuntimeError(f"不支持的远程投递渠道: {channel}")

        await adapter.send_text(
            IMOutboundMessage(
                source=IMMessageSource(
                    channel=channel,  # type: ignore[arg-type]
                    chat_id=chat_id,
                    chat_type="private",
                    user_id=chat_id,
                ),
                text=text,
            )
        )
