from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import FeishuConfig
from .models import IMEvent, IMMessageSource, IMOutboundMessage, IMStatus


StatusCallback = Callable[[IMStatus], Awaitable[None]]
EventCallback = Callable[[IMEvent], Awaitable[None]]


def _bind_lark_ws_loop(loop: asyncio.AbstractEventLoop) -> None:
    import lark_oapi.ws.client as ws_client

    ws_client.loop = loop


async def _disconnect_and_stop(client: Any) -> None:
    try:
        await asyncio.wait_for(client._disconnect(), timeout=3)
    except Exception:
        pass
    finally:
        asyncio.get_running_loop().stop()


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


class FeishuAdapter:
    def __init__(
        self,
        config: FeishuConfig,
        on_event: EventCallback,
        on_status: StatusCallback,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._on_status = on_status
        self._loop = asyncio.get_running_loop()
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws_client: Any | None = None
        self._api_client: Any | None = None
        self._stopped = False

    async def start(self) -> None:
        if not self._config.enabled:
            await self._emit_status("disabled", "飞书入口未启用。")
            return
        if not self._config.app_id or not self._config.app_secret:
            await self._emit_status("error", "飞书 App ID 或 App Secret 未配置。")
            return

        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        except ImportError:
            await self._emit_status(
                "error",
                "缺少 lark-oapi 依赖，请先同步后端依赖。",
            )
            return

        await self._emit_status("starting", "正在连接飞书长连接通道。")
        self._api_client = lark.Client.builder().app_id(self._config.app_id).app_secret(
            self._config.app_secret
        ).log_level(lark.LogLevel.INFO).build()

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message_event)
            .build()
        )
        self._ws_loop = asyncio.new_event_loop()
        _bind_lark_ws_loop(self._ws_loop)
        self._ws_client = lark.ws.Client(
            self._config.app_id,
            self._config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        self._thread = threading.Thread(
            target=self._run_ws_client,
            name="openEagle-feishu-ws",
            daemon=True,
        )
        self._thread.start()
        await self._emit_status("connected", "飞书长连接已启动。")

    async def stop(self) -> None:
        self._stopped = True
        client = self._ws_client
        loop = self._ws_loop
        if client is not None and loop is not None and not loop.is_closed():
            client._auto_reconnect = False
            if loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_disconnect_and_stop(client))
                )
        thread = self._thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 5)
        self._thread = None
        self._ws_client = None
        self._ws_loop = None
        await self._emit_status("disabled", "飞书长连接已停止。")

    async def send_text(self, message: IMOutboundMessage) -> None:
        if self._api_client is None:
            return
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )
        except ImportError:
            return

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(message.source.chat_id)
                .msg_type("text")
                .content(json.dumps({"text": message.text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._api_client.im.v1.message.create(request)
        if not response.success():
            detail = (
                f"飞书消息发送失败 code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}"
            )
            await self._emit_status("error", detail)

    def _run_ws_client(self) -> None:
        loop = self._ws_loop
        if loop is not None:
            asyncio.set_event_loop(loop)
            _bind_lark_ws_loop(loop)
        if self._stopped:
            return
        try:
            if self._ws_client is not None:
                self._ws_client.start()
        except Exception as exc:  # noqa: BLE001
            if not self._stopped:
                self._submit_status("error", f"飞书长连接异常: {exc}")
        finally:
            if loop is not None and not loop.is_closed():
                _close_loop(loop)

    def _handle_message_event(self, data: Any) -> None:
        event = parse_message_receive_event(data)
        if event is None:
            return
        asyncio.run_coroutine_threadsafe(self._on_event(event), self._loop)

    async def _emit_status(
        self,
        state: IMStatus.__annotations__["state"],
        detail: str,
        last_blocked_open_id: str | None = None,
        last_blocked_chat_id: str | None = None,
    ) -> None:
        await self._on_status(
            IMStatus(
                provider="feishu",
                state=state,
                detail=detail,
                last_blocked_open_id=last_blocked_open_id,
                last_blocked_chat_id=last_blocked_chat_id,
            )
        )

    def _submit_status(
        self,
        state: IMStatus.__annotations__["state"],
        detail: str,
    ) -> None:
        asyncio.run_coroutine_threadsafe(self._emit_status(state, detail), self._loop)


def parse_message_receive_event(data: Any) -> IMEvent | None:
    payload = _event_to_dict(data)
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    message_type = str(message.get("message_type") or "")
    if message_type != "text":
        return None

    chat_type_raw = str(message.get("chat_type") or "p2p").lower()
    chat_type = "group" if chat_type_raw == "group" else "private"
    text = _parse_text_content(message.get("content"))
    mentions = message.get("mentions") if isinstance(message.get("mentions"), list) else []
    if chat_type == "group":
        if not mentions:
            return None
        text = _strip_mention_tokens(text, mentions)

    text = text.strip()
    if not text:
        return None

    source = IMMessageSource(
        channel="feishu",
        chat_id=str(message.get("chat_id") or ""),
        chat_type=chat_type,
        user_id=str(sender_id.get("open_id") or sender_id.get("user_id") or ""),
        user_name=str(sender.get("sender_type") or ""),
        message_id=str(message.get("message_id") or ""),
    )
    if not source.chat_id or not source.user_id:
        return None
    return IMEvent(source=source, text=text, raw=payload)


def _event_to_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    try:
        import lark_oapi as lark

        marshalled = lark.JSON.marshal(data)
        if isinstance(marshalled, str):
            parsed = json.loads(marshalled)
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    raw = getattr(data, "__dict__", None)
    return raw if isinstance(raw, dict) else {}


def _parse_text_content(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(parsed, dict):
        return str(parsed.get("text") or "")
    return content


def _strip_mention_tokens(text: str, mentions: list[Any]) -> str:
    cleaned = text
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = str(mention.get("key") or "")
        if key:
            cleaned = cleaned.replace(key, "")
    return cleaned
