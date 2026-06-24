from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import WechatConfig
from .models import IMEvent, IMMessageSource, IMOutboundMessage, IMStatus

StatusCallback = Callable[[IMStatus], Awaitable[None]]
EventCallback = Callable[[IMEvent], Awaitable[None]]
BindStatusCallback = Callable[[dict[str, Any]], Awaitable[None]]

DEFAULT_WECHAT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_WECHAT_BOT_TYPE = "3"
WECHAT_LONG_POLL_TIMEOUT_MS = 35_000
WECHAT_RETRY_DELAY_SECONDS = 3


def _wechat_response_error(raw_text: str) -> str | None:
    raw_text = raw_text.strip()
    if not raw_text:
        return None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    def failed(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return not value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized not in {"", "0", "ok", "success", "succeeded"}
        return False

    for key in ("ret", "errcode", "code"):
        value = payload.get(key)
        if failed(value):
            raw_message = (
                payload.get("errmsg")
                or payload.get("message")
                or payload.get("msg")
                or ""
            )
            if str(value).strip() == "-2":
                return (
                    "微信 iLink 拒绝发送（ret=-2，通常是会话上下文过期或频率限制）。"
                    "请先在微信里给 openEagle 发一条消息刷新会话，再重试；"
                    f"如果仍失败，请重新扫码绑定。原始返回: {raw_text}"
                )
            message = (
                raw_message
                or raw_text
            )
            return f"{key}={value} {message}".strip()

    success = payload.get("success")
    if isinstance(success, bool) and not success:
        message = (
            payload.get("errmsg")
            or payload.get("message")
            or payload.get("msg")
            or raw_text
        )
        return str(message)
    return None


def ensure_open_eagle_clawbot_state_dir() -> Path:
    if not os.environ.get("OPENCLAW_STATE_DIR") and not os.environ.get("CLAWDBOT_STATE_DIR"):
        state_dir = Path.home() / ".openEagle" / "clawbot"
        os.environ["OPENCLAW_STATE_DIR"] = str(state_dir)
        return state_dir
    return Path(os.environ.get("OPENCLAW_STATE_DIR") or os.environ.get("CLAWDBOT_STATE_DIR") or "")


class WechatAdapter:
    def __init__(
        self,
        config: WechatConfig,
        on_event: EventCallback,
        on_status: StatusCallback,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._on_status = on_status
        self._task: asyncio.Task[None] | None = None
        self._bind_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._account: Any | None = None

    async def start(self) -> None:
        if not self._config.enabled:
            await self._emit_status("disabled", "微信 ClawBot 入口未启用。")
            return
        if not (self._config.account_id or "").strip():
            await self._emit_status("error", "微信 ClawBot 尚未扫码绑定。")
            return

        try:
            self._account = self._resolve_account()
        except Exception as exc:  # noqa: BLE001
            await self._emit_status("error", f"微信 ClawBot 账号解析失败: {exc}")
            return
        if not getattr(self._account, "configured", False) or not getattr(self._account, "token", None):
            await self._emit_status("error", "微信 ClawBot 凭据未找到，请重新扫码绑定。")
            return

        await self._emit_status("starting", "正在连接微信 ClawBot。")
        self._stopped = False
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._bind_task is not None and not self._bind_task.done():
            self._bind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bind_task
        self._bind_task = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._emit_status("disabled", "微信 ClawBot 入口已停止。")

    async def send_text(self, message: IMOutboundMessage) -> None:
        clean_text = message.text.strip()
        if clean_text:
            await self._send_plain_text(message.source, clean_text)
        if message.attachments:
            await self._send_plain_text(
                message.source,
                "微信 ClawBot 入口暂不支持发送附件，请回到 openEagle 查看附件内容。",
            )

    async def start_bind(
        self,
        emit_bind_status: BindStatusCallback,
        *,
        force: bool = False,
    ) -> None:
        if self._bind_task is not None and not self._bind_task.done():
            if not force:
                await emit_bind_status(
                    {
                        "state": "waiting",
                        "message": "已有微信扫码绑定正在等待确认。",
                    }
                )
                return
            await self.cancel_bind(emit_bind_status)

        try:
            ensure_open_eagle_clawbot_state_dir()
            from wechat_clawbot.auth.login_qr import start_weixin_login_with_qr

            start_result = await start_weixin_login_with_qr(
                api_base_url=self._base_url(),
                bot_type=self._bot_type(),
                account_id=(self._config.account_id or None),
                force=force,
            )
        except ImportError:
            await emit_bind_status(
                {
                    "state": "error",
                    "message": "缺少 wechat-clawbot 依赖，请先同步后端依赖。",
                }
            )
            return
        except Exception as exc:  # noqa: BLE001
            await emit_bind_status({"state": "error", "message": f"微信二维码获取失败: {exc}"})
            return

        if not getattr(start_result, "qrcode_url", None):
            await emit_bind_status(
                {
                    "state": "error",
                    "message": getattr(start_result, "message", "微信二维码获取失败。"),
                }
            )
            return

        await emit_bind_status(
            {
                "state": "qrcode",
                "message": getattr(start_result, "message", "请使用微信扫码绑定。"),
                "qrcodeUrl": start_result.qrcode_url,
            }
        )
        self._bind_task = asyncio.create_task(
            self._wait_for_bind(start_result.session_key, emit_bind_status)
        )

    async def cancel_bind(self, emit_bind_status: BindStatusCallback) -> None:
        if self._bind_task is not None and not self._bind_task.done():
            self._bind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bind_task
        self._bind_task = None
        await emit_bind_status({"state": "cancelled", "message": "微信扫码绑定已取消。"})

    async def unbind(self, emit_bind_status: BindStatusCallback) -> None:
        if self._bind_task is not None and not self._bind_task.done():
            await self.cancel_bind(emit_bind_status)
        await self.stop()
        account_id = (self._config.account_id or "").strip()
        if account_id:
            try:
                ensure_open_eagle_clawbot_state_dir()
                from wechat_clawbot.auth.accounts import (
                    clear_weixin_account,
                    normalize_account_id,
                    unregister_weixin_account_id,
                )
                from wechat_clawbot.messaging.inbound import clear_context_tokens_for_account

                normalized = normalize_account_id(account_id)
                clear_context_tokens_for_account(normalized)
                clear_weixin_account(normalized)
                unregister_weixin_account_id(normalized)
            except Exception as exc:  # noqa: BLE001
                await emit_bind_status({"state": "error", "message": f"微信解绑失败: {exc}"})
                return
        await emit_bind_status({"state": "unbound", "message": "微信 ClawBot 已解绑。"})

    async def _wait_for_bind(
        self,
        session_key: str,
        emit_bind_status: BindStatusCallback,
    ) -> None:
        await emit_bind_status({"state": "waiting", "message": "等待微信扫码确认。"})
        try:
            ensure_open_eagle_clawbot_state_dir()
            from wechat_clawbot.auth.accounts import (
                clear_stale_accounts_for_user_id,
                normalize_account_id,
                register_weixin_account_id,
                save_weixin_account,
            )
            from wechat_clawbot.auth.login_qr import wait_for_weixin_login
            from wechat_clawbot.messaging.inbound import clear_context_tokens_for_account

            wait_result = await wait_for_weixin_login(
                session_key=session_key,
                api_base_url=self._base_url(),
                bot_type=self._bot_type(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await emit_bind_status({"state": "error", "message": f"微信扫码绑定失败: {exc}"})
            return

        if not getattr(wait_result, "connected", False):
            await emit_bind_status(
                {
                    "state": "error",
                    "message": getattr(wait_result, "message", "微信扫码绑定失败。"),
                }
            )
            return
        if not getattr(wait_result, "account_id", None) or not getattr(wait_result, "bot_token", None):
            await emit_bind_status(
                {
                    "state": "error",
                    "message": "微信扫码绑定失败：服务器未返回完整账号凭据。",
                }
            )
            return

        account_id = normalize_account_id(wait_result.account_id)
        save_weixin_account(
            account_id,
            token=wait_result.bot_token,
            base_url=wait_result.base_url or self._base_url(),
            user_id=wait_result.user_id,
        )
        register_weixin_account_id(account_id)
        if wait_result.user_id:
            clear_stale_accounts_for_user_id(
                account_id,
                wait_result.user_id,
                on_clear_context_tokens=clear_context_tokens_for_account,
            )
        await emit_bind_status(
            {
                "state": "bound",
                "message": getattr(wait_result, "message", "微信 ClawBot 绑定成功。"),
                "accountId": account_id,
                "userId": wait_result.user_id,
            }
        )

    async def _poll_loop(self) -> None:
        assert self._account is not None
        try:
            ensure_open_eagle_clawbot_state_dir()
            from wechat_clawbot.api.client import get_updates
            from wechat_clawbot.messaging.inbound import restore_context_tokens
            from wechat_clawbot.storage.sync_buf import (
                get_sync_buf_file_path,
                load_get_updates_buf,
                save_get_updates_buf,
            )
        except ImportError:
            await self._emit_status("error", "缺少 wechat-clawbot 依赖，请先同步后端依赖。")
            return

        account_id = self._account.account_id
        restore_context_tokens(account_id)
        sync_buf_file = get_sync_buf_file_path(account_id)
        get_updates_buf = load_get_updates_buf(sync_buf_file) or ""
        await self._emit_status("connected", "微信 ClawBot 长轮询已启动。")

        while not self._stopped:
            try:
                resp = await get_updates(
                    base_url=self._account.base_url,
                    token=self._account.token,
                    get_updates_buf=get_updates_buf,
                    timeout_ms=WECHAT_LONG_POLL_TIMEOUT_MS,
                )
                is_error = (resp.ret is not None and resp.ret != 0) or (
                    resp.errcode is not None and resp.errcode != 0
                )
                if is_error:
                    await self._emit_status(
                        "error",
                        f"微信 getUpdates 失败: ret={resp.ret} errcode={resp.errcode} {resp.errmsg or ''}".strip(),
                    )
                    await asyncio.sleep(WECHAT_RETRY_DELAY_SECONDS)
                    continue

                if resp.get_updates_buf and resp.get_updates_buf != get_updates_buf:
                    get_updates_buf = resp.get_updates_buf
                    await asyncio.to_thread(save_get_updates_buf, sync_buf_file, get_updates_buf)

                for message in resp.msgs or []:
                    event = parse_weixin_message(message, account_id)
                    if event is not None:
                        await self._on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not self._stopped:
                    await self._emit_status("error", f"微信 ClawBot 长轮询异常: {exc}")
                    await asyncio.sleep(WECHAT_RETRY_DELAY_SECONDS)

    async def _send_plain_text(self, source: IMMessageSource, text: str) -> None:
        account = self._account or self._resolve_account()
        if not getattr(account, "configured", False) or not getattr(account, "token", None):
            await self._emit_status("error", "微信 ClawBot 凭据未找到，请重新扫码绑定。")
            raise RuntimeError("微信 ClawBot 凭据未找到，请重新扫码绑定。")
        try:
            from wechat_clawbot.api.client import (
                DEFAULT_API_TIMEOUT_MS,
                WeixinApiOptions,
                _api_post_fetch,
                _build_base_info,
                _dataclass_to_dict,
            )
            from wechat_clawbot.api.types import (
                MessageItem,
                MessageItemType,
                MessageState,
                MessageType,
                SendMessageReq,
                TextItem,
                WeixinMessage,
            )
            from wechat_clawbot.messaging.inbound import get_context_token, restore_context_tokens
        except ImportError as exc:
            await self._emit_status("error", "缺少 wechat-clawbot 依赖，请先同步后端依赖。")
            raise RuntimeError("缺少 wechat-clawbot 依赖，请先同步后端依赖。") from exc

        restore_context_tokens(account.account_id)
        opts = WeixinApiOptions(
            base_url=account.base_url,
            token=account.token,
            context_token=get_context_token(account.account_id, source.user_id),
        )
        client_id = f"open-eagle-{uuid4().hex}"
        req = SendMessageReq(
            msg=WeixinMessage(
                from_user_id="",
                to_user_id=source.user_id,
                client_id=client_id,
                message_type=MessageType.BOT,
                message_state=MessageState.FINISH,
                item_list=[
                    MessageItem(
                        type=MessageItemType.TEXT,
                        text_item=TextItem(text=text),
                    )
                ],
                context_token=opts.context_token or None,
            )
        )
        body = _dataclass_to_dict(req)
        body["base_info"] = _build_base_info()
        raw_response = await _api_post_fetch(
            base_url=opts.base_url,
            endpoint="ilink/bot/sendmessage",
            body=json.dumps(body),
            token=opts.token,
            timeout_ms=opts.timeout_ms or DEFAULT_API_TIMEOUT_MS,
            label="sendMessage",
        )
        response_error = _wechat_response_error(raw_response)
        if response_error:
            await self._emit_status("error", f"微信发送失败: {response_error}")
            raise RuntimeError(f"微信发送失败: {response_error}")

    def _resolve_account(self) -> Any:
        ensure_open_eagle_clawbot_state_dir()
        from wechat_clawbot.auth.accounts import resolve_weixin_account

        return resolve_weixin_account(account_id=self._config.account_id)

    def _base_url(self) -> str:
        return (self._config.base_url or DEFAULT_WECHAT_BASE_URL).strip() or DEFAULT_WECHAT_BASE_URL

    def _bot_type(self) -> str:
        return (self._config.bot_type or DEFAULT_WECHAT_BOT_TYPE).strip() or DEFAULT_WECHAT_BOT_TYPE

    async def _emit_status(
        self,
        state: IMStatus.__annotations__["state"],
        detail: str,
    ) -> None:
        await self._on_status(IMStatus(provider="wechat", state=state, detail=detail))


def parse_weixin_message(message: Any, account_id: str) -> IMEvent | None:
    try:
        ensure_open_eagle_clawbot_state_dir()
        from wechat_clawbot.api.types import MessageType
        from wechat_clawbot.messaging.inbound import body_from_item_list, set_context_token
    except ImportError:
        return None

    if getattr(message, "message_type", None) != MessageType.USER:
        return None

    text = body_from_item_list(getattr(message, "item_list", None)).strip()
    if not text:
        return None

    user_id = str(getattr(message, "from_user_id", None) or "")
    group_id = str(getattr(message, "group_id", None) or "")
    if not user_id:
        return None

    context_token = getattr(message, "context_token", None)
    if context_token:
        set_context_token(account_id, user_id, str(context_token))

    chat_id = group_id or user_id
    chat_type = "group" if group_id else "private"
    message_id = str(
        getattr(message, "message_id", None)
        or getattr(message, "seq", None)
        or context_token
        or ""
    )
    source = IMMessageSource(
        channel="wechat",
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_id.split("@", 1)[0],
        message_id=message_id,
    )
    raw = dataclasses.asdict(message) if dataclasses.is_dataclass(message) else {}
    return IMEvent(source=source, text=text, raw=raw)
