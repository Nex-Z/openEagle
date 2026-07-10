from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.8
DEFAULT_MAX_DELAY = 6.0

# 瞬时异常类名 -> 模块。两类 provider 共用相同类名，防御性导入：
# 库缺失或类名变动时降级为“不重试”，绝不因导入失败导致调用崩溃。
_TRANSIENT_SPEC: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openai", ("APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError")),
    ("anthropic", ("APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError")),
)


def _load_transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = []
    for module_name, names in _TRANSIENT_SPEC:
        try:
            module = __import__(module_name)
        except Exception:  # noqa: BLE001
            continue
        for name in names:
            cls = getattr(module, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                types.append(cls)
    return tuple(types)


_TRANSIENT_TYPES = _load_transient_types()


def is_retryable_llm_error(exc: BaseException) -> bool:
    """判断是否为值得退避重试的瞬时 LLM 错误（超时/连接/限流/5xx）。

    401/400/403/404 等非瞬时错误返回 False，由调用方立即上抛，不掩盖真实问题。
    """
    return bool(_TRANSIENT_TYPES) and isinstance(exc, _TRANSIENT_TYPES)


def _log(message: str) -> None:
    print(f"[BACKEND] {datetime.now(UTC).isoformat()} {message}", flush=True)


async def retry_llm_call(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    is_retryable: Callable[[BaseException], bool] = is_retryable_llm_error,
    label: str = "llm",
) -> Any:
    """对无副作用的 LLM HTTP 调用做带抖动的指数退避重试。

    coro_factory 每次调用返回一个新的 awaitable（不能重试已创建的协程）。
    只重试 is_retryable 判定为瞬时的异常；非瞬时异常立即上抛。
    耗尽后上抛最后一次异常，交由现有错误处理分支兜底（行为与重试前一致，只是更难触发）。
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.5 + random.random())
            _log(
                f"llm retry label={label} attempt={attempt}/{attempts} "
                f"delay={delay:.2f}s error={type(exc).__name__}: {exc}"
            )
            await asyncio.sleep(delay)
    # 理论不可达：循环要么 return 要么 raise。
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_llm_call 未能返回结果")
