from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from ..attachments import AttachmentStore, infer_attachment_kind
from ..config import TelegramConfig
from ..models import AttachmentRef
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
        if message.text.strip():
            for chunk in split_telegram_text(message.text):
                await self._api_call(
                    "sendMessage",
                    {
                        "chat_id": message.source.chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                )
        for attachment in message.attachments:
            await self._send_attachment(message, attachment)

    async def _send_attachment(
        self,
        message: IMOutboundMessage,
        attachment: AttachmentRef,
    ) -> None:
        if not attachment.local_path:
            return
        method = "sendPhoto" if attachment.kind == "image" else "sendDocument"
        field_name = "photo" if attachment.kind == "image" else "document"
        await asyncio.to_thread(
            self._api_call_multipart_sync,
            method,
            {
                "chat_id": message.source.chat_id,
            },
            field_name,
            attachment.local_path,
            attachment.name or os.path.basename(attachment.local_path),
            attachment.mime_type,
            60,
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

    async def download_event_attachments(
        self,
        event: IMEvent,
        attachment_store: AttachmentStore,
        conversation_id: str,
    ) -> list[AttachmentRef]:
        prepared: list[AttachmentRef] = []
        for item in event.attachments:
            try:
                prepared.append(await self._download_attachment(item, attachment_store, conversation_id))
            except Exception as exc:  # noqa: BLE001
                prepared.append(
                    item.model_copy(update={"status": "error", "error": f"Telegram 附件下载失败: {exc}"})
                )
        return prepared

    async def _download_attachment(
        self,
        item: AttachmentRef,
        attachment_store: AttachmentStore,
        conversation_id: str,
    ) -> AttachmentRef:
        file_id = str(item.remote_meta.get("fileId") or "")
        if not file_id:
            return item.model_copy(update={"status": "error", "error": "Telegram 附件缺少 fileId。"})
        file_info = await self._api_call("getFile", {"file_id": file_id})
        if not isinstance(file_info, dict) or not file_info.get("file_path"):
            return item.model_copy(update={"status": "error", "error": "Telegram getFile 未返回 file_path。"})
        file_path = str(file_info["file_path"])
        data = await asyncio.to_thread(self._download_file_sync, file_path)
        name = item.name or os.path.basename(file_path) or file_id
        mime_type = item.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return attachment_store.store_bytes(
            conversation_id,
            data=data,
            name=name,
            mime_type=mime_type,
            kind=item.kind,
            source="remote",
            remote_meta={**item.remote_meta, "provider": "telegram", "filePath": file_path},
            attachment_id=item.id,
        )

    def _download_file_sync(self, file_path: str) -> bytes:
        token = self._config.bot_token or ""
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read()

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

    def _api_call_multipart_sync(
        self,
        method: str,
        fields: dict[str, Any],
        file_field: str,
        file_path: str,
        filename: str,
        mime_type: str,
        timeout: int,
    ) -> Any:
        token = self._config.bot_token or ""
        url = f"https://api.telegram.org/bot{token}/{method}"
        boundary = f"----openEagle{os.urandom(12).hex()}"
        body = bytearray()
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {mime_type or 'application/octet-stream'}\r\n\r\n"
            ).encode("utf-8")
        )
        with open(file_path, "rb") as file_obj:
            body.extend(file_obj.read())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            url,
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
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
    text_value = message.get("text")
    caption_value = message.get("caption")
    text = text_value if isinstance(text_value, str) else caption_value if isinstance(caption_value, str) else ""
    attachments = _parse_telegram_attachments(message)
    if not text.strip() and not attachments:
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
    return IMEvent(source=source, text=cleaned, attachments=attachments, raw=update)


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


def _parse_telegram_attachments(message: dict[str, Any]) -> list[AttachmentRef]:
    attachments: list[AttachmentRef] = []
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = max(
            (item for item in photos if isinstance(item, dict)),
            key=lambda item: int(item.get("file_size") or 0),
            default=None,
        )
        if photo and photo.get("file_id"):
            file_id = str(photo.get("file_id"))
            attachments.append(
                AttachmentRef(
                    name=f"{file_id}.jpg",
                    mimeType="image/jpeg",
                    size=int(photo.get("file_size") or 0),
                    kind="image",
                    source="remote",
                    remoteMeta={
                        "provider": "telegram",
                        "fileId": file_id,
                        "messageId": str(message.get("message_id") or ""),
                        "messageType": "photo",
                    },
                    status="pending",
                )
            )

    for key, default_kind in (
        ("document", "file"),
        ("audio", "audio"),
        ("video", "video"),
        ("voice", "audio"),
    ):
        payload = message.get(key)
        if not isinstance(payload, dict) or not payload.get("file_id"):
            continue
        file_id = str(payload.get("file_id"))
        name = str(payload.get("file_name") or f"{key}-{file_id}")
        mime_type = str(payload.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream")
        kind = default_kind if default_kind != "file" else infer_attachment_kind(name, mime_type)
        attachments.append(
            AttachmentRef(
                name=name,
                mimeType=mime_type,
                size=int(payload.get("file_size") or 0),
                kind=kind,
                source="remote",
                remoteMeta={
                    "provider": "telegram",
                    "fileId": file_id,
                    "messageId": str(message.get("message_id") or ""),
                    "messageType": key,
                },
                status="pending",
            )
        )
    return attachments


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
