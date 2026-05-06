from __future__ import annotations

import re
import unittest

from app.config import AppConfig, FeishuConfig, TelegramConfig
from app.im.bridge import IMBridge, bind_config_getter
from app.im.commands import parse_im_command
from app.im.feishu import parse_message_receive_event
from app.im.models import IMEvent, IMMessageSource
from app.im.routing import build_conversation_binding, is_source_allowed
from app.im.telegram import parse_update, split_telegram_text
from app.models import AttachmentRef


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


class IMCommandTest(unittest.TestCase):
    def test_parse_plain_solo_chat_and_control_commands(self) -> None:
        self.assertEqual(parse_im_command("你好").name, "solo")
        self.assertEqual(parse_im_command("你好").argument, "你好")
        self.assertEqual(parse_im_command("/chat 你好").name, "chat")
        self.assertEqual(parse_im_command("/chat 你好").argument, "你好")
        self.assertEqual(parse_im_command("/pause").name, "pause")
        self.assertEqual(parse_im_command("/resume").name, "resume")
        self.assertEqual(parse_im_command("/stop").name, "stop")
        self.assertEqual(parse_im_command("/allow").name, "allow")
        self.assertEqual(parse_im_command("/reject").name, "reject")
        self.assertEqual(parse_im_command("/wat").name, "help")

    def test_chat_and_solo_require_argument(self) -> None:
        empty_chat = parse_im_command("/chat")
        empty = parse_im_command("/solo")
        task = parse_im_command("/solo 打开记事本")

        self.assertEqual(empty_chat.name, "help")
        self.assertEqual(empty.name, "help")
        self.assertEqual(task.name, "solo")
        self.assertEqual(task.argument, "打开记事本")

    def test_chat_and_solo_allow_empty_argument_for_attachments(self) -> None:
        empty_chat = parse_im_command("/chat", allow_empty_task=True)
        empty_solo = parse_im_command("/solo", allow_empty_task=True)

        self.assertEqual(empty_chat.name, "chat")
        self.assertEqual(empty_chat.argument, "")
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
        self.assertIn("附件处理失败", fake.sent[0].text)
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


if __name__ == "__main__":
    unittest.main()
