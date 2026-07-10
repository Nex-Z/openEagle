from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import llm_retry
from app.llm_retry import retry_llm_call


class _Transient(Exception):
    """测试用瞬时异常。"""


class _Fatal(Exception):
    """测试用非瞬时（致命）异常。"""


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, _Transient)


class RetryLlmCallTest(unittest.TestCase):
    def test_success_first_try_no_sleep(self) -> None:
        sleep_mock = AsyncMock()
        calls: list[int] = []

        async def factory() -> str:
            calls.append(1)
            return "ok"

        with patch.object(asyncio, "sleep", new=sleep_mock), patch.object(
            llm_retry.random, "random", return_value=0.0
        ):
            result = asyncio.run(
                retry_llm_call(factory, attempts=3, is_retryable=_is_transient, label="t")
            )
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)
        sleep_mock.assert_not_called()

    def test_retries_on_transient_then_succeeds(self) -> None:
        sleep_mock = AsyncMock()
        attempts: list[int] = []

        async def factory() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise _Transient("blip")
            return "ok"

        with patch.object(asyncio, "sleep", new=sleep_mock), patch.object(
            llm_retry.random, "random", return_value=0.0
        ):
            result = asyncio.run(
                retry_llm_call(factory, attempts=3, is_retryable=_is_transient, label="t")
            )
        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep_mock.await_count, 2)

    def test_raises_immediately_on_non_retryable(self) -> None:
        sleep_mock = AsyncMock()
        attempts: list[int] = []

        async def factory() -> str:
            attempts.append(1)
            raise _Fatal("bad request")

        with patch.object(asyncio, "sleep", new=sleep_mock), patch.object(
            llm_retry.random, "random", return_value=0.0
        ):
            with self.assertRaises(_Fatal):
                asyncio.run(
                    retry_llm_call(factory, attempts=3, is_retryable=_is_transient, label="t")
                )
        self.assertEqual(len(attempts), 1)
        sleep_mock.assert_not_called()

    def test_raises_after_exhausting_attempts(self) -> None:
        sleep_mock = AsyncMock()
        attempts: list[int] = []

        async def factory() -> str:
            attempts.append(1)
            raise _Transient("sustained")

        with patch.object(asyncio, "sleep", new=sleep_mock), patch.object(
            llm_retry.random, "random", return_value=0.0
        ):
            with self.assertRaises(_Transient):
                asyncio.run(
                    retry_llm_call(factory, attempts=3, is_retryable=_is_transient, label="t")
                )
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep_mock.await_count, 2)

    def test_is_retryable_llm_error_uses_loaded_transient_types(self) -> None:
        # 自定义异常不在已加载的 provider 瞬时类型里 -> False
        self.assertFalse(llm_retry.is_retryable_llm_error(_Transient("x")))
        original = llm_retry._TRANSIENT_TYPES
        llm_retry._TRANSIENT_TYPES = (_Transient,)
        try:
            self.assertTrue(llm_retry.is_retryable_llm_error(_Transient("x")))
            self.assertFalse(llm_retry.is_retryable_llm_error(_Fatal("x")))
        finally:
            llm_retry._TRANSIENT_TYPES = original


if __name__ == "__main__":
    unittest.main()
