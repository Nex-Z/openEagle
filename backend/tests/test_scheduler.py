from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig
from app.scheduler.models import ScheduledTask
from app.scheduler.service import SchedulerService
from app.scheduler.store import create_task, get_task, init_db


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


if __name__ == "__main__":
    unittest.main()
