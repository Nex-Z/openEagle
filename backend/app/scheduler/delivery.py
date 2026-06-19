from __future__ import annotations

from ..config import AppConfig
from ..im.bridge import resolve_channel_config
from .models import ScheduledTask


def prepare_scheduled_task_delivery(
    task: ScheduledTask,
    app_config: AppConfig,
    local_conversation_id: str,
) -> None:
    channel = (task.im_channel or "").strip().lower()
    chat_id = (task.im_chat_id or "").strip()
    if not channel:
        task.im_channel = None
        task.im_chat_id = None
        task.conversation_id = local_conversation_id
        return

    channel_labels = {
        "feishu": "飞书",
        "telegram": "Telegram",
        "wechat": "微信",
    }
    if channel not in channel_labels:
        raise ValueError(f"不支持的定时任务投递渠道: {channel}")
    if not chat_id:
        raise ValueError(f"{channel_labels[channel]}投递必须填写接收会话 ID")

    channel_config = resolve_channel_config(app_config, channel)
    if task.enabled and not channel_config.enabled:
        raise ValueError(f"{channel_labels[channel]}尚未启用，不能作为定时任务投递渠道")
    if task.enabled and channel == "feishu" and not (
        getattr(channel_config, "app_id", None)
        and getattr(channel_config, "app_secret", None)
    ):
        raise ValueError("飞书缺少 App ID 或 App Secret，不能作为定时任务投递渠道")
    if task.enabled and channel == "telegram" and not getattr(
        channel_config, "bot_token", None
    ):
        raise ValueError("Telegram 缺少 Bot Token，不能作为定时任务投递渠道")
    if task.enabled and channel == "wechat" and not getattr(
        channel_config, "account_id", None
    ):
        raise ValueError("微信尚未完成扫码绑定，不能作为定时任务投递渠道")

    task.im_channel = channel
    task.im_chat_id = chat_id
    task.conversation_id = None
