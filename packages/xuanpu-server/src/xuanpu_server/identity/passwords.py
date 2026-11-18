"""口令摘要与口令规则（ADR 0019 决策 3）。

摘要用 ``argon2-cffi`` 的 argon2id，参数取库的默认值（RFC 9106 低内存档）。慢哈希是 CPU 与内存
密集的同步计算，放进线程池执行，免得卡住事件循环；全进程同时最多算 ``MAX_CONCURRENT_HASHES`` 个，
排队超过 ``QUEUE_SECONDS`` 报 503——匿名的登录请求驱动不了无限的哈希计算。

登录时用户名不存在也要算一次摘要（``verify_absent``），让「无此人」与「口令错」耗时相近，不能据此
探测用户名。存储里的摘要格式不对（不是本库认得的 PHC 字符串）是数据损坏，记日志并报内部错误，
不当作口令错误吞掉。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeVar

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from xuanpu_server.errors import InvalidRequestError, PlatformError, RuntimeUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")

MIN_PASSWORD_LENGTH = 10
"""口令的最短长度（字符）。不强制字符种类组合，长度比组合更有效（NIST SP 800-63B）。"""
MAX_PASSWORD_LENGTH = 128
"""口令的最长长度（字符）：防止超长输入拖慢哈希。"""
MAX_CONCURRENT_HASHES = 4
"""全进程同时计算的摘要数；每个约占 64 MiB 内存。"""
QUEUE_SECONDS = 10.0
"""等待计算名额的最长时间（秒），超过报 503。"""
_ABSENT_PASSWORD = "xuanpu-absent-user-placeholder"  # noqa: S105 —— 只用来对齐耗时，不是口令


def validate_new_password(password: str, *, username: str) -> None:
    """新口令必须满足的规则：长度在 10–128 个字符之间，且不能与用户名相同（不区分大小写）。

    Raises:
        InvalidRequestError: 不满足规则；提示说明哪一条。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidRequestError(f"口令至少 {MIN_PASSWORD_LENGTH} 个字符")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidRequestError(f"口令最多 {MAX_PASSWORD_LENGTH} 个字符")
    if password.strip().lower() == username.lower():
        raise InvalidRequestError("口令不能与用户名相同")


class PasswordHashing:
    """口令摘要的计算与核对，带全进程的并发上限。

    Attributes:
        hasher: argon2 的哈希器；测试可以传低成本参数的实例加快用例。
    """

    def __init__(
        self,
        hasher: PasswordHasher | None = None,
        *,
        max_concurrent: int = MAX_CONCURRENT_HASHES,
        queue_seconds: float = QUEUE_SECONDS,
    ) -> None:
        """
        Args:
            hasher: argon2 哈希器；省略用库的默认参数。
            max_concurrent: 同时计算的摘要数上限。
            queue_seconds: 等待名额的最长时间。
        """
        self.hasher = hasher or PasswordHasher()
        self._slots = anyio.Semaphore(max_concurrent)
        self._queue_seconds = queue_seconds
        self._absent_hash: str | None = None

    async def _run(self, func: Callable[[], T]) -> T:
        """拿到名额后在线程里执行；排队超时报 503。"""
        try:
            with anyio.fail_after(self._queue_seconds):
                await self._slots.acquire()
        except TimeoutError as exc:
            raise RuntimeUnavailableError("登录请求过多，请稍后重试") from exc
        try:
            return await anyio.to_thread.run_sync(func)
        finally:
            self._slots.release()

    async def hash(self, password: str) -> str:
        """计算口令的摘要（PHC 字符串，含盐与参数）。"""
        return await self._run(lambda: self.hasher.hash(password))

    async def verify(self, stored: str, password: str) -> tuple[bool, str | None]:
        """核对口令，返回 ``(是否匹配, 重算后的摘要)``。

        匹配且库里的参数已经过时（``check_needs_rehash``）时，顺手用当前参数重算一份返回，由调用方
        写回；否则第二项为 None。

        Raises:
            PlatformError: 存储里的摘要格式不对（数据损坏）。
        """

        def check() -> tuple[bool, str | None]:
            try:
                self.hasher.verify(stored, password)
            except VerifyMismatchError:
                return False, None
            except InvalidHashError as exc:
                logger.error("存储里的口令摘要格式不对，无法核对")
                raise PlatformError("服务内部错误") from exc
            except VerificationError:
                return False, None
            if self.hasher.check_needs_rehash(stored):
                return True, self.hasher.hash(password)
            return True, None

        return await self._run(check)

    async def verify_absent(self, password: str) -> None:
        """用户名不存在（或没设口令）时也核对一次，耗时与真核对相近；结果丢弃。"""
        if self._absent_hash is None:
            self._absent_hash = await self.hash(_ABSENT_PASSWORD)
        absent = self._absent_hash

        def check() -> None:
            try:
                self.hasher.verify(absent, password)
            except VerifyMismatchError:
                return

        await self._run(check)


__all__ = [
    "MAX_CONCURRENT_HASHES",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "QUEUE_SECONDS",
    "PasswordHashing",
    "validate_new_password",
]
