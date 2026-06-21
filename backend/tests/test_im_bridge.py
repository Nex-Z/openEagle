from __future__ import annotations

import asyncio
import re
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.config import AppConfig, ContextConfig, FeishuConfig, TelegramConfig, WechatConfig
from app.im.bridge import IMBridge, IM_RECEIVED_ACK_TEXT, bind_config_getter
from app.im.commands import parse_im_command
from app.im.feishu import parse_message_receive_event
from app.im.models import IMEvent, IMMessageSource
from app.im.outbound import RemoteOutboundService
from app.im.routing import build_conversation_binding, is_source_allowed
from app.im.telegram import parse_update, split_telegram_text
from app.im.wechat import WechatAdapter, parse_weixin_message
from app.models import AttachmentRef
from wechat_clawbot.api.types import (
    MessageItem,
    MessageItemType,
    MessageState,
    MessageType,
    TextItem,
    WeixinMessage,
)


class IMRoutingTest(unittest.TestCase):
    def test_conversation_id_is_tauri_safe(self) -> None:
        source = IMMessageSource(
            channel="feishu",
            chat_id="oc_xxx/with spaces",
            chat_type="group",
            user_id="ou_user",
        )

        binding = build_conversation_binding(source)

        self.assertRegex(binding.conversation_id, r"^im_feishu_[A-Fa-f0-9]{24}$")
        self.assertIsNotNone(re.fullmatch(r"[A-Za-z0-9_-]+", binding.conversation_id))

    def test_empty_whitelist_rejects_everything(self) -> None:
        source = IMMessageSource(
            channel="feishu",
            chat_id="oc_chat",
            chat_type="private",
            user_id="ou_user",
        )

        self.assertFalse(is_source_allowed(FeishuConfig(enabled=True), source))

    def test_open_id_or_chat_id_allow_source(self) -> None:
        source = IMMessageSource(
            channel="feishu",
            chat_id="oc_chat",
            chat_type="group",
            user_id="ou_user",
        )

        self.assertTrue(
            is_source_allowed(
                FeishuConfig(enabled=True, allowedOpenIds=["ou_user"]),
                source,
            )
        )
        self.assertTrue(
            is_source_allowed(
                FeishuConfig(enabled=True, allowedChatIds=["oc_chat"]),
                source,
            )
        )

    def test_telegram_user_id_or_chat_id_allow_source(self) -> None:
        source = IMMessageSource(
            channel="telegram",
            chat_id="-100123",
            chat_type="group",
            user_id="42",
        )

        self.assertTrue(
            is_source_allowed(
                TelegramConfig(enabled=True, allowedUserIds=["42"]),
                source,
            )
        )
        self.assertTrue(
            is_source_allowed(
                TelegramConfig(enabled=True, allowedChatIds=["-100123"]),
                source,
            )
        )

    def test_wechat_user_id_or_chat_id_allow_source(self) -> None:
        source = IMMessageSource(
            channel="wechat",
            chat_id="wx_group",
            chat_type="group",
            user_id="wx_user",
        )

        self.assertTrue(
            is_source_allowed(
                WechatConfig(enabled=True, allowedUserIds=["wx_user"]),
                source,
            )
        )
        self.assertTrue(
            is_source_allowed(
                WechatConfig(enabled=True, allowedChatIds=["wx_group"]),
                source,
            )
        )


class IMCommandTest(unittest.TestCase):
    def test_parse_plain_auto_solo_and_control_commands(self) -> None:
        self.assertEqual(parse_im_command("你好").name, "auto")
        self.assertEqual(parse_im_command("你好").argument, "你好")
        self.assertEqual(parse_im_command("/chat 你好").name, "help")
        self.assertEqual(parse_im_command("/pause").name, "pause")
        self.assertEqual(parse_im_command("/resume").name, "resume")
        self.assertEqual(parse_im_command("/stop").name, "stop")
        self.assertEqual(parse_im_command("/allow").name, "allow")
        self.assertEqual(parse_im_command("/reject").name, "reject")
        self.assertEqual(parse_im_command("/wat").name, "help")

    def test_solo_requires_argument(self) -> None:
        empty = parse_im_command("/solo")
        task = parse_im_command("/solo 打开记事本")

        self.assertEqual(empty.name, "help")
        self.assertEqual(task.name, "solo")
        self.assertEqual(task.argument, "打开记事本")

    def test_solo_allows_empty_argument_for_attachments(self) -> None:
        empty_solo = parse_im_command("/solo", allow_empty_task=True)

        self.assertEqual(empty_solo.name, "solo")
        self.assertEqual(empty_solo.argument, "")


class FakeIMAdapter:
    def __init__(self, prepared_attachments: list[AttachmentRef] | None = None) -> None:
        self.prepared_attachments = prepared_attachments
        self.sent = []

    async def send_text(self, message) -> None:
        self.sent.append(message)

    async def download_event_attachments(self, event, attachment_store, conversation_id):
        return self.prepared_attachments if self.prepared_attachments is not None else event.attachments


class FakeAttachmentStore:
    def public_dicts(self, attachments):
        return [item.model_dump(by_alias=True, exclude_none=True) for item in attachments]


class RemoteOutboundServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_delivery_does_not_require_live_im_bridge(self) -> None:
        sent = []

        class FakeWechatAdapter:
            def __init__(self, config, on_event, on_status) -> None:
                self.config = config

            async def send_text(self, message) -> None:
                sent.append(message)

        service = RemoteOutboundService(
            lambda: AppConfig(
                wechat=WechatConfig(enabled=True, accountId="wx-account"),
            )
        )
        with patch("app.im.outbound.WechatAdapter", FakeWechatAdapter):
            await service.send_text("wechat", "wx_user", "定时任务结果")

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].source.user_id, "wx_user")
        self.assertEqual(sent[0].text, "定时任务结果")


class IMBridgeScheduledDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_target_can_be_sent_without_live_conversation_binding(self) -> None:
        fake = FakeIMAdapter()
        bridge = IMBridge(
            send_client=lambda *args: _record_async([], args),
            handle_chat=lambda *args: _record_async([], args, result="chat"),
            start_solo=lambda *args: _record_async([], args, result="solo"),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
        )
        bridge._telegram_adapter = fake

        await bridge.send_to_target("telegram", "-100123", "定时任务结果")

        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0].source.channel, "telegram")
        self.assertEqual(fake.sent[0].source.chat_id, "-100123")
        self.assertEqual(fake.sent[0].text, "定时任务结果")

    def test_delivery_target_uses_original_remote_chat(self) -> None:
        bridge = IMBridge(
            send_client=lambda *args: _record_async([], args),
            handle_chat=lambda *args: _record_async([], args, result="chat"),
            start_solo=lambda *args: _record_async([], args, result="solo"),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
        )
        binding = build_conversation_binding(
            IMMessageSource(
                channel="feishu",
                chat_id="oc_chat",
                chat_type="group",
                user_id="ou_user",
            )
        )
        bridge._bindings[binding.conversation_id] = binding

        self.assertEqual(
            bridge.delivery_target(binding.conversation_id),
            ("feishu", "oc_chat"),
        )


class IMBridgeAttachmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_download_error_does_not_call_chat(self) -> None:
        chat_calls = []
        client_events = []
        fake = FakeIMAdapter(
            [
                AttachmentRef(
                    name="too-large.pdf",
                    kind="file",
                    source="remote",
                    status="error",
                    error="超过 25MB 限制",
                )
            ]
        )
        bridge = IMBridge(
            send_client=lambda *args: _record_async(client_events, args),
            handle_chat=lambda *args: _record_async(chat_calls, args, result="chat"),
            start_solo=lambda *args: _record_async([], args, result="solo"),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
            attachment_store=FakeAttachmentStore(),
        )
        bridge._telegram_adapter = fake
        bind_config_getter(
            bridge,
            lambda: AppConfig(
                telegram=TelegramConfig(enabled=True, allowedUserIds=["42"])
            ),
        )

        await bridge._handle_event(
            IMEvent(
                source=IMMessageSource(
                    channel="telegram",
                    chat_id="42",
                    chat_type="private",
                    user_id="42",
                    message_id="9",
                ),
                text="",
                attachments=[AttachmentRef(name="too-large.pdf", source="remote")],
            )
        )

        self.assertEqual(chat_calls, [])
        self.assertEqual(fake.sent[0].text, IM_RECEIVED_ACK_TEXT)
        self.assertIn("附件处理失败", fake.sent[1].text)
        self.assertTrue(any(event[0] == "server:message" for event in client_events))

    async def test_solo_command_with_only_attachment_uses_default_task(self) -> None:
        solo_calls = []
        fake = FakeIMAdapter(
            [
                AttachmentRef(
                    name="report.pdf",
                    mimeType="application/pdf",
                    size=123,
                    kind="file",
                    source="remote",
                    localPath="E:/tmp/report.pdf",
                    status="ready",
                )
            ]
        )
        bridge = IMBridge(
            send_client=lambda *args: _record_async([], args),
            handle_chat=lambda *args: _record_async([], args, result="chat"),
            start_solo=lambda *args: _record_async(solo_calls, args, result="solo started"),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
            attachment_store=FakeAttachmentStore(),
        )
        bridge._telegram_adapter = fake
        bind_config_getter(
            bridge,
            lambda: AppConfig(
                telegram=TelegramConfig(enabled=True, allowedUserIds=["42"])
            ),
        )

        await bridge._handle_event(
            IMEvent(
                source=IMMessageSource(
                    channel="telegram",
                    chat_id="42",
                    chat_type="private",
                    user_id="42",
                    message_id="10",
                ),
                text="/solo",
                attachments=[AttachmentRef(name="report.pdf", source="remote")],
            )
        )

        self.assertEqual(len(solo_calls), 1)
        self.assertIn("请结合附件执行任务", solo_calls[0][1])
        self.assertIn("附件列表", solo_calls[0][1])

    async def test_solo_ack_is_sent_once_when_start_returns_ack(self) -> None:
        fake = FakeIMAdapter()
        bridge = IMBridge(
            send_client=lambda *args: _record_async([], args),
            handle_chat=lambda *args: _record_async([], args, result="chat"),
            start_solo=lambda *args: _record_async([], args, result=IM_RECEIVED_ACK_TEXT),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
            attachment_store=FakeAttachmentStore(),
        )
        bridge._telegram_adapter = fake
        bind_config_getter(
            bridge,
            lambda: AppConfig(
                telegram=TelegramConfig(enabled=True, allowedUserIds=["42"])
            ),
        )

        await bridge._handle_event(
            IMEvent(
                source=IMMessageSource(
                    channel="telegram",
                    chat_id="42",
                    chat_type="private",
                    user_id="42",
                    message_id="11",
                ),
                text="/solo 打开记事本",
            )
        )

        self.assertEqual([message.text for message in fake.sent], [IM_RECEIVED_ACK_TEXT])

    async def test_plain_conversation_routes_to_main_agent_without_processing_ack(self) -> None:
        chat_calls = []
        solo_calls = []
        fake = FakeIMAdapter()
        bridge = IMBridge(
            send_client=lambda *args: _record_async([], args),
            handle_chat=lambda *args: _record_async(chat_calls, args, result="AI 生成的自然回复"),
            start_solo=lambda *args: _record_async(solo_calls, args, result="solo"),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
            attachment_store=FakeAttachmentStore(),
        )
        bridge._telegram_adapter = fake
        bind_config_getter(
            bridge,
            lambda: AppConfig(
                telegram=TelegramConfig(enabled=True, allowedUserIds=["42"])
            ),
        )

        await bridge._handle_event(
            IMEvent(
                source=IMMessageSource(
                    channel="telegram",
                    chat_id="42",
                    chat_type="private",
                    user_id="42",
                    message_id="12",
                ),
                text="你好",
            )
        )

        self.assertEqual(len(chat_calls), 1)
        self.assertEqual(chat_calls[0][1], "你好")
        self.assertEqual(solo_calls, [])
        self.assertEqual([message.text for message in fake.sent], ["AI 生成的自然回复"])

    async def test_idle_im_conversation_compacts_before_continuing(self) -> None:
        chat_calls = []
        compact_calls = []
        fake = FakeIMAdapter()
        bridge = IMBridge(
            send_client=lambda *args: _record_async([], args),
            handle_chat=lambda *args: _record_async(chat_calls, args, result="ok"),
            start_solo=lambda *args: _record_async([], args, result="solo"),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
            attachment_store=FakeAttachmentStore(),
            compact_context=lambda conversation_id: _record_async(
                compact_calls,
                (conversation_id,),
                result=True,
            ),
        )
        bridge._telegram_adapter = fake
        bind_config_getter(
            bridge,
            lambda: AppConfig(
                telegram=TelegramConfig(enabled=True, allowedUserIds=["42"]),
                context=ContextConfig(imIdleCleanupMinutes=5),
            ),
        )
        source = IMMessageSource(
            channel="telegram",
            chat_id="42",
            chat_type="private",
            user_id="42",
            message_id="13",
        )
        binding = build_conversation_binding(source)
        bridge._last_activity_at[binding.conversation_id] = datetime.now(UTC) - timedelta(minutes=10)

        await bridge._handle_event(IMEvent(source=source, text="回来继续"))

        self.assertEqual(len(chat_calls), 1)
        self.assertEqual(chat_calls[0][1], "回来继续")
        self.assertEqual(compact_calls, [(binding.conversation_id,)])
        bridge._cancel_idle_compaction(binding.conversation_id)

    async def test_idle_timer_compacts_in_background(self) -> None:
        compact_calls = []
        bridge = IMBridge(
            send_client=lambda *args: _record_async([], args),
            handle_chat=lambda *args: _record_async([], args, result="ok"),
            start_solo=lambda *args: _record_async([], args, result="solo"),
            solo_control=lambda *args: _record_async([], args, result="control"),
            tool_decision=lambda *args: _record_async([], args, result="tool"),
            compact_context=lambda conversation_id: _record_async(
                compact_calls,
                (conversation_id,),
                result=True,
            ),
        )
        bind_config_getter(
            bridge,
            lambda: AppConfig(context=ContextConfig(imIdleCleanupMinutes=5)),
        )
        activity_at = datetime.now(UTC) - timedelta(minutes=10)
        bridge._last_activity_at["im_demo"] = activity_at

        with patch("app.im.bridge.asyncio.sleep", return_value=None):
            await bridge._compact_after_idle("im_demo", activity_at, 5)

        self.assertEqual(compact_calls, [("im_demo",)])


async def _record_async(target, args, result=None):
    target.append(args)
    return result


class FeishuEventParseTest(unittest.TestCase):
    def test_private_text_message_parses(self) -> None:
        event = parse_message_receive_event(
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
                    "message": {
                        "message_id": "om_msg",
                        "chat_id": "oc_chat",
                        "chat_type": "p2p",
                        "message_type": "text",
                        "content": '{"text":"hello"}',
                    },
                }
            }
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.chat_type, "private")
        self.assertEqual(event.text, "hello")

    def test_group_message_requires_mention_and_strips_token(self) -> None:
        without_mention = parse_message_receive_event(
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                    "message": {
                        "chat_id": "oc_chat",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": '{"text":"hello"}',
                    },
                }
            }
        )
        with_mention = parse_message_receive_event(
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                    "message": {
                        "chat_id": "oc_chat",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": '{"text":"@_user_1 /help"}',
                        "mentions": [{"key": "@_user_1"}],
                    },
                }
            }
        )

        self.assertIsNone(without_mention)
        self.assertIsNotNone(with_mention)
        self.assertEqual(with_mention.text, "/help")

    def test_image_message_parses_attachment(self) -> None:
        event = parse_message_receive_event(
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                    "message": {
                        "message_id": "om_msg",
                        "chat_id": "oc_chat",
                        "chat_type": "p2p",
                        "message_type": "image",
                        "content": '{"image_key":"img_key"}',
                    },
                }
            }
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "")
        self.assertEqual(event.attachments[0].kind, "image")
        self.assertEqual(event.attachments[0].remote_meta["fileKey"], "img_key")


class TelegramEventParseTest(unittest.TestCase):
    def test_private_text_update_parses(self) -> None:
        event = parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "from": {"id": 42, "first_name": "Ada"},
                    "chat": {"id": 42, "type": "private"},
                    "text": "hello",
                },
            },
            bot_username="openEagleBot",
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.source.channel, "telegram")
        self.assertEqual(event.source.chat_type, "private")
        self.assertEqual(event.text, "hello")

    def test_group_update_requires_bot_mention(self) -> None:
        without_mention = parse_update(
            {
                "update_id": 1,
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": -100123, "type": "supergroup"},
                    "text": "hello",
                },
            },
            bot_username="openEagleBot",
        )
        with_mention = parse_update(
            {
                "update_id": 2,
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": -100123, "type": "group"},
                    "text": "@openEagleBot /help",
                },
            },
            bot_username="openEagleBot",
        )

        self.assertIsNone(without_mention)
        self.assertIsNotNone(with_mention)
        self.assertEqual(with_mention.text, "/help")

    def test_group_command_requires_bot_username_suffix(self) -> None:
        plain = parse_update(
            {
                "update_id": 1,
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": -100123, "type": "group"},
                    "text": "/help",
                },
            },
            bot_username="openEagleBot",
        )
        targeted = parse_update(
            {
                "update_id": 2,
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": -100123, "type": "group"},
                    "text": "/solo@openEagleBot 打开记事本",
                },
            },
            bot_username="openEagleBot",
        )

        self.assertIsNone(plain)
        self.assertIsNotNone(targeted)
        self.assertEqual(targeted.text, "/solo 打开记事本")

    def test_telegram_text_split_uses_api_limit(self) -> None:
        chunks = split_telegram_text("x" * 4100)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 4096)
        self.assertEqual(len(chunks[1]), 4)

    def test_private_document_update_parses_attachment_without_text(self) -> None:
        event = parse_update(
            {
                "update_id": 3,
                "message": {
                    "message_id": 9,
                    "from": {"id": 42, "first_name": "Ada"},
                    "chat": {"id": 42, "type": "private"},
                    "document": {
                        "file_id": "file-1",
                        "file_name": "report.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 123,
                    },
                },
            },
            bot_username="openEagleBot",
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.text, "")
        self.assertEqual(event.attachments[0].name, "report.pdf")
        self.assertEqual(event.attachments[0].remote_meta["fileId"], "file-1")


class WechatEventParseTest(unittest.TestCase):
    def test_private_text_message_parses(self) -> None:
        event = parse_weixin_message(
            WeixinMessage(
                seq=1,
                message_id=99,
                from_user_id="wx_user",
                message_type=MessageType.USER,
                message_state=MessageState.FINISH,
                item_list=[
                    MessageItem(
                        type=MessageItemType.TEXT,
                        text_item=TextItem(text="hello"),
                    )
                ],
                context_token="ctx-1",
            ),
            "account-1",
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.source.channel, "wechat")
        self.assertEqual(event.source.chat_type, "private")
        self.assertEqual(event.source.chat_id, "wx_user")
        self.assertEqual(event.text, "hello")

    def test_group_text_message_uses_group_chat_id(self) -> None:
        event = parse_weixin_message(
            WeixinMessage(
                seq=1,
                from_user_id="wx_user",
                group_id="wx_group",
                message_type=MessageType.USER,
                item_list=[
                    MessageItem(
                        type=MessageItemType.TEXT,
                        text_item=TextItem(text="/help"),
                    )
                ],
            ),
            "account-1",
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.source.chat_type, "group")
        self.assertEqual(event.source.chat_id, "wx_group")


class WechatOutboundContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_restores_persisted_context_token_before_delivery(self) -> None:
        restored = []
        sent = []

        class FakeAccount:
            configured = True
            token = "token"
            account_id = "wx-account"
            base_url = "https://example.test"

        async def fake_send(to, text, opts):
            sent.append((to, text, opts.context_token))
            return {"messageId": "message-1"}

        adapter = WechatAdapter(
            WechatConfig(enabled=True, accountId="wx-account"),
            on_event=lambda *_args: _record_async([], ()),
            on_status=lambda *_args: _record_async([], ()),
        )
        adapter._account = FakeAccount()
        source = IMMessageSource(
            channel="wechat",
            chat_id="wx-user",
            chat_type="private",
            user_id="wx-user",
        )

        with (
            patch(
                "wechat_clawbot.messaging.inbound.restore_context_tokens",
                lambda account_id: restored.append(account_id),
            ),
            patch(
                "wechat_clawbot.messaging.inbound.get_context_token",
                lambda account_id, user_id: "ctx-restored",
            ),
            patch("wechat_clawbot.messaging.send.send_message_weixin", fake_send),
        ):
            await adapter._send_plain_text(source, "测试消息")

        self.assertEqual(restored, ["wx-account"])
        self.assertEqual(sent, [("wx-user", "测试消息", "ctx-restored")])


class WechatBindTest(unittest.IsolatedAsyncioTestCase):
    async def test_qr_bind_success_saves_account_and_emits_bound(self) -> None:
        statuses = []
        saved = []
        registered = []

        class StartResult:
            qrcode_url = "weixin://qr/abc"
            message = "scan"
            session_key = "session-1"

        class WaitResult:
            connected = True
            message = "ok"
            account_id = "wx@im.bot"
            bot_token = "token-1"
            base_url = "https://example.test"
            user_id = "wx_user"

        async def fake_start(**kwargs):
            return StartResult()

        async def fake_wait(**kwargs):
            return WaitResult()

        async def emit(payload):
            statuses.append(payload)

        adapter = WechatAdapter(
            WechatConfig(enabled=False, botType="3"),
            on_event=lambda event: _record_async([], (event,)),
            on_status=lambda status: _record_async([], (status,)),
        )

        with (
            patch("wechat_clawbot.auth.login_qr.start_weixin_login_with_qr", fake_start),
            patch("wechat_clawbot.auth.login_qr.wait_for_weixin_login", fake_wait),
            patch("wechat_clawbot.auth.accounts.save_weixin_account", lambda *args, **kwargs: saved.append((args, kwargs))),
            patch("wechat_clawbot.auth.accounts.register_weixin_account_id", lambda account_id: registered.append(account_id)),
            patch("wechat_clawbot.auth.accounts.clear_stale_accounts_for_user_id", lambda *args, **kwargs: None),
        ):
            await adapter.start_bind(emit, force=True)
            assert adapter._bind_task is not None
            await adapter._bind_task

        self.assertEqual(statuses[0]["state"], "qrcode")
        self.assertEqual(statuses[-1]["state"], "bound")
        self.assertEqual(statuses[-1]["accountId"], "wx-im-bot")
        self.assertEqual(saved[0][0][0], "wx-im-bot")
        self.assertEqual(registered, ["wx-im-bot"])

    async def test_unbind_clears_account_files(self) -> None:
        cleared = []
        unregistered = []
        context_cleared = []
        statuses = []

        async def emit(payload):
            statuses.append(payload)

        adapter = WechatAdapter(
            WechatConfig(enabled=False, accountId="wx-im-bot"),
            on_event=lambda event: _record_async([], (event,)),
            on_status=lambda status: _record_async([], (status,)),
        )

        with (
            patch("wechat_clawbot.auth.accounts.clear_weixin_account", lambda account_id: cleared.append(account_id)),
            patch("wechat_clawbot.auth.accounts.unregister_weixin_account_id", lambda account_id: unregistered.append(account_id)),
            patch("wechat_clawbot.messaging.inbound.clear_context_tokens_for_account", lambda account_id: context_cleared.append(account_id)),
        ):
            await adapter.unbind(emit)

        self.assertEqual(cleared, ["wx-im-bot"])
        self.assertEqual(unregistered, ["wx-im-bot"])
        self.assertEqual(context_cleared, ["wx-im-bot"])
        self.assertEqual(statuses[-1]["state"], "unbound")

    async def test_stop_cancels_pending_qr_bind(self) -> None:
        bind_statuses = []
        adapter_statuses = []
        wait_cancelled = asyncio.Event()

        class StartResult:
            qrcode_url = "weixin://qr/abc"
            message = "scan"
            session_key = "session-1"

        async def fake_start(**kwargs):
            return StartResult()

        async def fake_wait(**kwargs):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                wait_cancelled.set()
                raise

        async def emit(payload):
            bind_statuses.append(payload)

        adapter = WechatAdapter(
            WechatConfig(enabled=False, botType="3"),
            on_event=lambda event: _record_async([], (event,)),
            on_status=lambda status: _record_async(adapter_statuses, (status,)),
        )

        with (
            patch("wechat_clawbot.auth.login_qr.start_weixin_login_with_qr", fake_start),
            patch("wechat_clawbot.auth.login_qr.wait_for_weixin_login", fake_wait),
        ):
            await adapter.start_bind(emit, force=True)
            assert adapter._bind_task is not None
            await asyncio.sleep(0)
            await adapter.stop()

        self.assertTrue(wait_cancelled.is_set())
        self.assertIsNone(adapter._bind_task)
        self.assertEqual(bind_statuses[0]["state"], "qrcode")
        self.assertNotIn("bound", [item["state"] for item in bind_statuses])
        self.assertEqual(adapter_statuses[-1][0].state, "disabled")


if __name__ == "__main__":
    unittest.main()
