from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig, FeishuConfig, TelegramConfig, WechatConfig
from app.scheduler.delivery import prepare_scheduled_task_delivery
from app.scheduler.models import ScheduledTask, ScheduledTaskExecution
from app.scheduler.service import SchedulerService
from app.scheduler.store import (
    create_execution,
    create_task,
    get_history,
    get_task,
    init_db,
    list_tasks,
    update_task_next_run,
)
from app.scheduler.tools import (
    create_scheduled_task,
    set_scheduled_task_origin_resolver,
    set_scheduler_service,
)


class SchedulerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "scheduler.db"
        init_db(self._db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_start_reloads_persisted_enabled_tasks(self) -> None:
        task = ScheduledTask(
            name="Yearly check",
            prompt="Say hello.",
            scheduleExpr="0 0 1 1 *",
        )
        create_task(task)

        async def run_check() -> None:
            service = SchedulerService(config_getter=AppConfig)
            service.start()
            try:
                job = service._scheduler.get_job(f"scheduled-task-{task.id}")
                persisted = get_task(task.id)

                self.assertIsNotNone(job)
                self.assertIsNotNone(persisted)
                self.assertIsNotNone(persisted.next_run_at)
            finally:
                service.shutdown()

        asyncio.run(run_check())

    def test_sync_next_run_replaces_stale_persisted_value(self) -> None:
        task = ScheduledTask(
            name="Daily check",
            prompt="Say hello.",
            scheduleExpr="0 8 * * *",
        )
        create_task(task)

        async def run_check() -> None:
            service = SchedulerService(config_getter=AppConfig)
            service.start()
            try:
                update_task_next_run(task.id, "stale")
                service._sync_next_run(task.id)
                job = service._scheduler.get_job(f"scheduled-task-{task.id}")
                persisted = get_task(task.id)

                self.assertIsNotNone(job)
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.next_run_at, job.next_run_time.isoformat())
            finally:
                service.shutdown()

        asyncio.run(run_check())

    def test_manual_run_accepts_disabled_task_and_rejects_duplicate(self) -> None:
        task = ScheduledTask(
            name="Paused task",
            prompt="Say hello.",
            scheduleExpr="0 8 * * *",
            enabled=False,
        )
        create_task(task)

        async def run_check() -> None:
            service = SchedulerService(config_getter=AppConfig)
            started = asyncio.Event()
            release = asyncio.Event()

            async def fake_execute(
                task_id: str,
                *,
                allow_disabled: bool = False,
                already_claimed: bool = False,
            ) -> ScheduledTaskExecution | None:
                self.assertEqual(task_id, task.id)
                self.assertTrue(allow_disabled)
                self.assertTrue(already_claimed)
                started.set()
                try:
                    await release.wait()
                finally:
                    service._running_task_ids.discard(task_id)
                return None

            service._execute_task = fake_execute  # type: ignore[method-assign]
            first_run = service.trigger_task_now(task.id)
            await started.wait()

            with self.assertRaisesRegex(RuntimeError, "正在执行"):
                service.trigger_task_now(task.id)

            release.set()
            await first_run
            await asyncio.sleep(0)
            self.assertNotIn(task.id, service._running_task_ids)
            self.assertEqual(service._manual_tasks, set())

        asyncio.run(run_check())

    def test_manual_run_rejects_missing_task(self) -> None:
        service = SchedulerService(config_getter=AppConfig)

        async def run_check() -> None:
            with self.assertRaisesRegex(ValueError, "不存在"):
                service.trigger_task_now("missing")

        asyncio.run(run_check())

    def test_delivery_failure_marks_execution_failed_and_preserves_result(self) -> None:
        task = ScheduledTask(
            name="微信日报",
            prompt="生成日报",
            scheduleExpr="0 8 * * *",
            imChannel="wechat",
            imChatId="wx_user",
        )
        create_task(task)
        execution = ScheduledTaskExecution(taskId=task.id)
        create_execution(execution)

        async def send_remote(*_args) -> None:
            raise RuntimeError("微信发送失败")

        service = SchedulerService(
            config_getter=AppConfig,
            send_remote=send_remote,
        )
        asyncio.run(service._complete_and_deliver(task, execution, "日报正文"))

        history = get_history(task.id)
        self.assertEqual(history[0].status, "failed")
        self.assertEqual(history[0].result, "日报正文")
        self.assertIn("结果投递失败", history[0].error or "")
        self.assertIn("微信发送失败", history[0].error or "")

    def test_remote_delivery_without_sender_raises(self) -> None:
        task = ScheduledTask(
            name="微信提醒",
            prompt="提醒",
            scheduleExpr="0 9 * * *",
            imChannel="wechat",
            imChatId="wx_user",
        )
        service = SchedulerService(config_getter=AppConfig)

        with self.assertRaisesRegex(RuntimeError, "远程投递服务未初始化"):
            asyncio.run(service._deliver_result(task, result="提醒内容"))

    def test_remote_origin_is_persisted_when_agent_creates_task(self) -> None:
        registered: list[ScheduledTask] = []

        class FakeSchedulerService:
            def add_task(self, task: ScheduledTask) -> None:
                registered.append(task)

        set_scheduler_service(FakeSchedulerService())  # type: ignore[arg-type]
        set_scheduled_task_origin_resolver(
            lambda conversation_id: (
                ("telegram", "-100123") if conversation_id == "im_telegram_test" else None
            )
        )
        try:
            result = create_scheduled_task(
                name="远程日报",
                prompt="生成日报",
                schedule_expr="0 20 * * *",
                conversation_id="im_telegram_test",
            )
        finally:
            set_scheduled_task_origin_resolver(None)

        persisted = list_tasks()
        self.assertIn("已创建定时任务", result)
        self.assertEqual(len(registered), 1)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].conversation_id, "im_telegram_test")
        self.assertEqual(persisted[0].im_channel, "telegram")
        self.assertEqual(persisted[0].im_chat_id, "-100123")


class SchedulerDeliveryConfigTest(unittest.TestCase):
    def test_local_delivery_uses_current_client_conversation(self) -> None:
        task = ScheduledTask(
            name="本地日报",
            prompt="生成日报",
            scheduleExpr="0 20 * * *",
        )

        prepare_scheduled_task_delivery(task, AppConfig(), "conversation-local")

        self.assertEqual(task.conversation_id, "conversation-local")
        self.assertIsNone(task.im_channel)
        self.assertIsNone(task.im_chat_id)

    def test_enabled_remote_channel_is_normalized(self) -> None:
        task = ScheduledTask(
            name="远程日报",
            prompt="生成日报",
            scheduleExpr="0 20 * * *",
            imChannel="telegram",
            imChatId=" -100123 ",
        )
        config = AppConfig(
            telegram=TelegramConfig(enabled=True, botToken="token"),
        )

        prepare_scheduled_task_delivery(task, config, "conversation-local")

        self.assertIsNone(task.conversation_id)
        self.assertEqual(task.im_channel, "telegram")
        self.assertEqual(task.im_chat_id, "-100123")

    def test_disabled_or_unconfigured_remote_channel_is_rejected(self) -> None:
        cases = [
            (
                ScheduledTask(
                    name="飞书",
                    prompt="提醒",
                    scheduleExpr="0 9 * * *",
                    imChannel="feishu",
                    imChatId="oc_chat",
                ),
                AppConfig(feishu=FeishuConfig(enabled=False)),
                "尚未启用",
            ),
            (
                ScheduledTask(
                    name="Telegram",
                    prompt="提醒",
                    scheduleExpr="0 9 * * *",
                    imChannel="telegram",
                    imChatId="42",
                ),
                AppConfig(telegram=TelegramConfig(enabled=True)),
                "缺少 Bot Token",
            ),
            (
                ScheduledTask(
                    name="微信",
                    prompt="提醒",
                    scheduleExpr="0 9 * * *",
                    imChannel="wechat",
                    imChatId="wx_user",
                ),
                AppConfig(wechat=WechatConfig(enabled=True)),
                "尚未完成扫码绑定",
            ),
        ]

        for task, config, expected in cases:
            with self.subTest(channel=task.im_channel):
                with self.assertRaisesRegex(ValueError, expected):
                    prepare_scheduled_task_delivery(task, config, "conversation-local")


class SchedulerDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_result_is_delivered_only_to_selected_remote_target(self) -> None:
        local_events = []
        remote_messages = []

        async def send_event(*args) -> None:
            local_events.append(args)

        async def send_remote(*args) -> None:
            remote_messages.append(args)

        service = SchedulerService(
            config_getter=AppConfig,
            send_event=send_event,
            send_remote=send_remote,
        )
        task = ScheduledTask(
            name="远程日报",
            prompt="生成日报",
            scheduleExpr="0 20 * * *",
            conversationId="im_telegram_test",
            imChannel="telegram",
            imChatId="-100123",
        )

        await service._deliver_result(task, result="日报内容")

        self.assertEqual(local_events, [])
        self.assertEqual(
            remote_messages,
            [("telegram", "-100123", "【定时任务：远程日报】\n\n日报内容")],
        )

    async def test_local_result_is_delivered_to_client_conversation(self) -> None:
        local_events = []

        async def send_event(*args) -> None:
            local_events.append(args)

        service = SchedulerService(
            config_getter=AppConfig,
            send_event=send_event,
        )
        task = ScheduledTask(
            name="本地提醒",
            prompt="提醒",
            scheduleExpr="0 9 * * *",
            conversationId="conversation-local",
        )

        await service._deliver_result(task, result="该开会了")

        self.assertEqual(local_events[0][0], "server:scheduled_task_executed")
        self.assertEqual(local_events[0][2], "conversation-local")


if __name__ == "__main__":
    unittest.main()
