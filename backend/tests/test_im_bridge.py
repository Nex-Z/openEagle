from __future__ import annotations

import re
import unittest

from app.config import FeishuConfig
from app.im.commands import parse_im_command
from app.im.feishu import parse_message_receive_event
from app.im.models import IMMessageSource
from app.im.routing import build_conversation_binding, is_source_allowed


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


class IMCommandTest(unittest.TestCase):
    def test_parse_plain_chat_and_control_commands(self) -> None:
        self.assertEqual(parse_im_command("你好").name, "chat")
        self.assertEqual(parse_im_command("/pause").name, "pause")
        self.assertEqual(parse_im_command("/resume").name, "resume")
        self.assertEqual(parse_im_command("/stop").name, "stop")
        self.assertEqual(parse_im_command("/allow").name, "allow")
        self.assertEqual(parse_im_command("/reject").name, "reject")

    def test_solo_requires_argument(self) -> None:
        empty = parse_im_command("/solo")
        task = parse_im_command("/solo 打开记事本")

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


if __name__ == "__main__":
    unittest.main()
