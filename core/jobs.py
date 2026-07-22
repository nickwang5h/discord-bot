import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    initial_delay_seconds: float = 60
    backoff_factor: float = 2
    max_delay_seconds: float = 300

    def __post_init__(self):
        if self.attempts < 1:
            raise ValueError("attempts 必须至少为 1")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds 不能为负数")


async def retry_async(
    task_name: str,
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = RetryPolicy(),
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> T:
    delay = policy.initial_delay_seconds
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except Exception as error:
            if attempt >= policy.attempts:
                logger.exception("[%s] 连续 %s 次执行失败", task_name, policy.attempts)
                raise
            logger.warning(
                "[%s] 第 %s/%s 次执行失败: %s；%.1f 秒后重试",
                task_name,
                attempt,
                policy.attempts,
                error,
                delay,
            )
            await sleep(delay)
            delay = min(delay * policy.backoff_factor, policy.max_delay_seconds)


async def run_delivery_job(
    *,
    lock: asyncio.Lock,
    task_name: str,
    build: Callable[[], Awaitable[T | None]],
    deliver: Callable[[T], Awaitable[object]],
    on_delivered: Callable[[T], object] | None = None,
    retry_policy: RetryPolicy = RetryPolicy(),
) -> T | None:
    """Run a single-flight job whose build is retryable and delivery is at-most-once."""
    if lock.locked():
        logger.warning("[%s] 已有任务执行中，跳过重复触发", task_name)
        return None

    async with lock:
        payload = await retry_async(task_name, build, policy=retry_policy)
        if payload is None:
            return None

        await deliver(payload)
        if on_delivered is not None:
            result = on_delivered(payload)
            if inspect.isawaitable(result):
                await result
        return payload
