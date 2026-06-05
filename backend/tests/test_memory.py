from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import AgentConfig, AppConfig
from app.memory import MemoryService, init_db
from app.memory.models import DEFAULT_AGENT_SOUL_CORE


class MemoryServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "memory.db"
        init_db(self._db_path)
        self.service = MemoryService()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_soul_core_is_seeded(self) -> None:
        state = self.service.state()
        self.assertEqual(state.agent_soul.core, DEFAULT_AGENT_SOUL_CORE)
        self.assertIn("Be concise, but not sterile.", state.agent_soul.core)

        context = self.service.prompt_context(max_chars=8000)
        self.assertIn("Soul 摘要", context)
        self.assertIn("concise but not sterile", context)
        self.assertNotIn("# SOUL.md - Who You Are", context)

    def test_legacy_default_soul_core_is_upgraded(self) -> None:
        legacy_core = (
            "# SOUL.md - Who You Are\n\n"
            "_You're not a chatbot. You're an agent that acts._\n"
        )
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                "UPDATE agent_soul SET core = ? WHERE id = ?",
                (legacy_core, "default"),
            )
            conn.commit()
        finally:
            conn.close()

        init_db(self._db_path)

        state = self.service.state()
        self.assertEqual(state.agent_soul.core, DEFAULT_AGENT_SOUL_CORE)

    def test_manual_empty_soul_core_is_not_reseeded(self) -> None:
        self.service.save_manual({"agentSoul": {"core": ""}})

        init_db(self._db_path)

        state = self.service.state()
        self.assertEqual(state.agent_soul.core, "")

    def test_manual_save_updates_profile_notes_and_soul(self) -> None:
        self.service.save_manual(
            {
                "profile": {"content": "用户喜欢简洁中文。"},
                "agentSoul": {"core": "保持聪明、温暖、直接。", "sideNotes": "少说套话。"},
                "notes": [
                    {
                        "id": "note-1",
                        "text": "用户正在做 openEagle。",
                        "tags": ["project"],
                        "confidence": 0.9,
                        "status": "active",
                    }
                ],
            }
        )

        state = self.service.state()
        self.assertIn("简洁中文", state.profile.content)
        self.assertIn("温暖", state.agent_soul.core)
        self.assertEqual(state.agent_soul.side_notes, "少说套话。")
        self.assertEqual(state.notes[0].id, "note-1")
        self.assertEqual(state.notes[0].tags, ["project"])
        self.assertGreaterEqual(len(state.audit), 3)

    def test_prompt_context_is_bounded_and_active_only(self) -> None:
        self.service.save_manual(
            {
                "profile": {"content": "A" * 400},
                "notes": [
                    {"id": "note-a", "text": "活跃笔记", "status": "active"},
                    {"id": "note-b", "text": "归档笔记", "status": "archived"},
                ],
                "agentSoul": {"core": "手动 Soul core", "sideNotes": "自动旁注"},
            }
        )

        context = self.service.prompt_context(query="活跃笔记", max_chars=800)
        self.assertLessEqual(len(context), 800)
        self.assertIn("用户画像", context)
        self.assertIn("活跃笔记", context)
        self.assertNotIn("归档笔记", context)

    def test_prompt_context_uses_relevant_notes(self) -> None:
        self.service.save_manual(
            {
                "notes": [
                    {
                        "id": "note-anime-wed",
                        "text": "遮天：每周三更新",
                        "tags": ["anime"],
                        "status": "active",
                    },
                    {
                        "id": "note-project",
                        "text": "openEagle 正在迁移记忆系统",
                        "tags": ["project"],
                        "status": "active",
                    },
                    {
                        "id": "note-archived",
                        "text": "归档动漫笔记",
                        "tags": ["anime"],
                        "status": "archived",
                    },
                ]
            }
        )

        context = self.service.prompt_context(query="今天有什么动漫更新", max_notes=2)

        self.assertIn("相关用户笔记", context)
        self.assertIn("遮天：每周三更新", context)
        self.assertNotIn("openEagle 正在迁移记忆系统", context)
        self.assertNotIn("归档动漫笔记", context)

    def test_prompt_context_does_not_fallback_to_recent_notes_without_query(self) -> None:
        self.service.save_manual(
            {
                "notes": [
                    {
                        "id": "note-recent",
                        "text": "最近但不相关的用户笔记",
                        "status": "active",
                    }
                ]
            }
        )

        context = self.service.prompt_context()

        self.assertNotIn("近期用户笔记", context)
        self.assertNotIn("最近但不相关的用户笔记", context)

    def test_prompt_context_filters_stale_side_notes(self) -> None:
        self.service.apply_distillation(
            {
                "agentSideNotes": (
                    "当用户询问今天有什么动漫更新时，需要先确认当前星期几，因为无法自动获取日期。\n"
                    "回答要自然一点。"
                )
            }
        )

        context = self.service.prompt_context(query="今天有什么动漫更新")

        self.assertIn("回答要自然一点", context)
        self.assertNotIn("无法自动获取日期", context)
        self.assertNotIn("需要先确认当前星期几", context)

    def test_record_turn_redacts_secrets_and_distill_without_model_is_noop(self) -> None:
        event_id = self.service.record_turn(
            conversation_id="conv",
            request_id="req",
            user_content="api_key=sk-secretsecretsecretsecret",
            assistant_content="done",
            route="answer_directly",
        )

        state = self.service.state()
        self.assertEqual(state.events[0].id, event_id)
        self.assertIn("[redacted-secret]", state.events[0].content)
        self.assertFalse(asyncio.run(self.service.distill_event(event_id)))

    def test_snapshot_redacts_json_shaped_secret_payloads(self) -> None:
        self.service.ingest_snapshot(
            conversation_id="conv",
            request_id="req",
            source="test",
            content='{"password":"secret123","apiKey":"sk-secretsecretsecretsecret"}',
            payload={
                "password": "secret123",
                "apiKey": "sk-secretsecretsecretsecret",
                "nested": {
                    "authorization": "Bearer abcdefghijklmnop",
                    "notes": ["token=super-secret-value"],
                },
            },
        )

        event = self.service.state().events[0]
        serialized_payload = json.dumps(event.payload, ensure_ascii=False)
        self.assertIn("[redacted-secret]", event.content)
        self.assertIn("[redacted-secret]", serialized_payload)
        self.assertNotIn("secret123", event.content)
        self.assertNotIn("secret123", serialized_payload)
        self.assertNotIn("sk-secretsecretsecretsecret", serialized_payload)
        self.assertNotIn("abcdefghijklmnop", serialized_payload)

    def test_auto_distillation_preserves_manual_profile_and_notes(self) -> None:
        self.service.save_manual(
            {
                "profile": {"content": "手动画像"},
                "notes": [
                    {
                        "id": "note-manual",
                        "text": "手动笔记",
                        "tags": ["manual"],
                        "confidence": 1,
                        "status": "active",
                    }
                ],
            }
        )

        changed = self.service.apply_distillation(
            {
                "profile": "自动画像",
                "agentSideNotes": "自动旁注",
                "notes": [
                    {"action": "archive", "id": "note-manual", "reason": "过时"},
                    {
                        "action": "update",
                        "id": "note-manual",
                        "text": "自动覆盖",
                        "tags": ["auto"],
                        "confidence": 0.2,
                    },
                    {
                        "action": "add",
                        "text": "自动新增",
                        "tags": ["auto"],
                        "confidence": 0.7,
                    },
                ],
            },
            source="auto:event-1",
        )

        state = self.service.state()
        manual_note = next(note for note in state.notes if note.id == "note-manual")
        self.assertTrue(changed)
        self.assertEqual(state.profile.content, "手动画像")
        self.assertEqual(manual_note.text, "手动笔记")
        self.assertEqual(manual_note.status, "active")
        self.assertEqual(manual_note.source, "manual")
        self.assertIn("自动旁注", state.agent_soul.side_notes)
        self.assertTrue(any(note.text == "自动新增" for note in state.notes))

    def test_manual_save_marks_only_changed_notes_manual(self) -> None:
        self.service.apply_distillation(
            {
                "notes": [
                    {
                        "id": "note-auto",
                        "text": "自动笔记",
                        "tags": ["auto"],
                        "confidence": 0.7,
                    }
                ]
            },
            source="auto:event-1",
        )

        self.service.save_manual(
            {
                "profile": {"content": "只改画像"},
                "notes": [
                    {
                        "id": "note-auto",
                        "text": "自动笔记",
                        "tags": ["auto"],
                        "confidence": 0.7,
                        "status": "active",
                    }
                ],
            }
        )
        unchanged_note = self.service.state().notes[0]
        self.assertEqual(unchanged_note.source, "auto:event-1")

        self.service.save_manual(
            {
                "notes": [
                    {
                        "id": "note-auto",
                        "text": "用户改过的笔记",
                        "tags": ["manual"],
                        "confidence": 0.9,
                        "status": "active",
                    }
                ],
            }
        )
        changed_note = self.service.state().notes[0]
        self.assertEqual(changed_note.source, "manual")
        self.assertEqual(changed_note.text, "用户改过的笔记")

    def test_manual_save_archives_notes_missing_from_payload(self) -> None:
        self.service.save_manual(
            {
                "notes": [
                    {"id": "note-keep", "text": "保留笔记", "status": "active"},
                    {"id": "note-delete", "text": "删除笔记", "status": "active"},
                ]
            }
        )

        self.service.save_manual(
            {
                "notes": [
                    {"id": "note-keep", "text": "保留笔记", "status": "active"},
                ]
            }
        )

        state = self.service.state()
        keep = next(note for note in state.notes if note.id == "note-keep")
        deleted = next(note for note in state.notes if note.id == "note-delete")
        payload = self.service.state_payload()

        self.assertEqual(keep.status, "active")
        self.assertEqual(deleted.status, "archived")
        self.assertEqual([note["id"] for note in payload["notes"]], ["note-keep"])

    def test_distillation_applies_model_json(self) -> None:
        service = MemoryService(
            config_getter=lambda: AppConfig(
                agent=AgentConfig(provider="openai", apiKey="test-key")
            )
        )
        event_id = service.record_turn(
            conversation_id="conv",
            request_id="req",
            user_content="我喜欢短答案。",
            assistant_content="记住了。",
            route="answer_directly",
        )

        with patch.object(
            MemoryService,
            "_distill_with_model",
            new=AsyncMock(
                return_value='{"profile":"用户喜欢短答案。","agentSideNotes":"回答要短。","notes":[{"action":"add","text":"偏好短答案","tags":["preference"],"confidence":0.8}]}'
            ),
        ):
            changed = asyncio.run(service.distill_event(event_id))

        state = service.state()
        self.assertTrue(changed)
        self.assertIn("短答案", state.profile.content)
        self.assertIn("回答要短", state.agent_soul.side_notes)
        self.assertEqual(state.notes[0].text, "偏好短答案")

    def test_bad_distillation_json_returns_false(self) -> None:
        service = MemoryService(
            config_getter=lambda: AppConfig(
                agent=AgentConfig(provider="openai", apiKey="test-key")
            )
        )
        event_id = service.record_turn(
            conversation_id="conv",
            request_id="req",
            user_content="hello",
            assistant_content="hi",
        )
        with patch.object(
            MemoryService,
            "_distill_with_model",
            new=AsyncMock(return_value="not json"),
        ):
            changed = asyncio.run(service.distill_event(event_id))

        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
