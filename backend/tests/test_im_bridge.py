from __future__ import annotations

import re
import unittest

from app.config import FeishuConfig, TelegramConfig
from app.im.commands import parse_im_command
from app.im.feishu import parse_message_receive_event
from app.im.models import IMMessageSource
from app.im.routing import build_conversation_binding, is_source_allowed
from app.im.telegram import parse_update, split_telegram_text


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

    def test_non_text_message_is_ignored(self) -> None:
        event = parse_message_receive_event(
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                    "message": {
                        "chat_id": "oc_chat",
                        "chat_type": "p2p",
                        "message_type": "image",
                    },
                }
            }
        )

        self.assertIsNone(event)


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


if __name__ == "__main__":
    unittest.main()
