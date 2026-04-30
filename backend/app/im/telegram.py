from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import TelegramConfig
from .models import IMEvent, IMMessageSource, IMOutboundMessage, IMStatus

TELEGRAM_MESSAGE_LIMIT = 4096

StatusCallback = Callable[[IMStatus], Awaitable[None]]
EventCallback = Callable[[IMEvent], Awaitable[None]]


class TelegramAdapter:
    def __init__(
        self,
        config: TelegramConfig,
        on_event: EventCallback,
        on_status: StatusCallback,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._on_status = on_status
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._offset: int | None = None
        self._bot_username = ""

    async def start(self) -> None:
        if not self._config.enabled:
            await self._emit_status("disabled", "Telegram 入口未启用。")
            return
        if not self._config.bot_token:
            await self._emit_status("error", "Telegram Bot Token 未配置。")
            return

        await self._emit_status("starting", "正在连接 Telegram Bot API。")
        try:
            me = await self._api_call("getMe")
            if isinstance(me, dict):
                self._bot_username = str(me.get("username") or "")
            await self._api_call("deleteWebhook", {"drop_pending_updates": False})
        except Exception as exc:  # noqa: BLE001
            await self._emit_status("error", f"Telegram 初始化失败: {exc}")
            return

        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._emit_status("disabled", "Telegram 入口已停止。")

    async def send_text(self, message: IMOutboundMessage) -> None:
        for chunk in split_telegram_text(message.text):
            await self._api_call(
                "sendMessage",
                {
                    "chat_id": message.source.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )

    async def _poll_loop(self) -> None:
        await self._emit_status("connected", "Telegram 长轮询已启动。")
        while not self._stopped:
            try:
                updates = await self._api_call(
                    "getUpdates",
                    {
                        "timeout": 30,
                        "offset": self._offset,
                        "allowed_updates": ["message"],
                    },
                    timeout=35,
                )
                if not isinstance(updates, list):
                    continue
                for update in updates:
                    if isinstance(update, dict):
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            self._offset = update_id + 1
                        event = parse_update(update, self._bot_username)
                        if event is not None:
                            await self._on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not self._stopped:
                    await self._emit_status("error", f"Telegram 长轮询异常: {exc}")
                    await asyncio.sleep(3)

    async def _api_call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> Any:
        clean_payload = {
            key: value for key, value in (payload or {}).items() if value is not None
        }
        return await asyncio.to_thread(self._api_call_sync, method, clean_payload, timeout)

    def _api_call_sync(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: int,
    ) -> Any:
        token = self._config.bot_token or ""
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            raise RuntimeError(str(parsed))
        return parsed.get("result")

    async def _emit_status(
        self,
        state: IMStatus.__annotations__["state"],
        detail: str,
    ) -> None:
        await self._on_status(IMStatus(provider="telegram", state=state, detail=detail))


def parse_update(update: dict[str, Any], bot_username: str = "") -> IMEvent | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = str(chat.get("id") or "")
    user_id = str(sender.get("id") or "")
    if not chat_id or not user_id:
        return None

    chat_type_raw = str(chat.get("type") or "private")
    chat_type = "private" if chat_type_raw == "private" else "group"
    cleaned = text.strip()
    if chat_type == "group":
        cleaned = _extract_group_text(cleaned, bot_username)
        if not cleaned:
            return None

    first = str(sender.get("first_name") or "")
    last = str(sender.get("last_name") or "")
    username = str(sender.get("username") or "")
    display_name = " ".join([first, last]).strip() or username
    source = IMMessageSource(
        channel="telegram",
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=display_name,
        message_id=str(message.get("message_id") or ""),
    )
    return IMEvent(source=source, text=cleaned, raw=update)


def split_telegram_text(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    chunks: list[str] = []
    remaining = stripped
    while remaining:
        chunks.append(remaining[:TELEGRAM_MESSAGE_LIMIT])
        remaining = remaining[TELEGRAM_MESSAGE_LIMIT:]
    return chunks


def _extract_group_text(text: str, bot_username: str) -> str:
    username = bot_username.lstrip("@").lower()
    command_match = re.match(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(\s+.*)?$", text)
    if command_match:
        mentioned = (command_match.group(2) or "").lower()
        if not username or mentioned != username:
            return ""
        tail = command_match.group(3) or ""
        return f"/{command_match.group(1)}{tail}".strip()

    if not username:
        return ""
    mention = f"@{username}"
    lowered = text.lower()
    if mention not in lowered:
        return ""
    pattern = re.compile(re.escape(mention), re.IGNORECASE)
    return pattern.sub("", text).strip()
