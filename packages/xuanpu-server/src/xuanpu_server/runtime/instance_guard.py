"""首期单实例持有权：生产使用专用数据库连接，试用使用文件锁。"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
from typing import TYPE_CHECKING, BinaryIO, Protocol

from xuanpu_server.errors import ConflictError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import asyncpg


class InstanceGuard(Protocol):
    """在启动后台工作前获取、关闭所有工作后释放的所有权。"""

    on_lost: Callable[[], None] | None

    @property
    def lost(self) -> bool:
        """持有权是否意外丢失。"""
        ...

    async def acquire(self) -> None:
        """拒绝同一运行域的第二个实例。"""
        ...

    async def close(self) -> None:
        """幂等释放持有权。"""
        ...


class FileInstanceGuard:
    """仅约束同一数据目录的本机试用实例。"""

    lost = False

    def __init__(self, directory: Path) -> None:
        """锁文件留在数据目录，不保存凭据。"""
        self._directory = directory
        self._file: BinaryIO | None = None
        self.on_lost: Callable[[], None] | None = None

    def _acquire(self) -> None:
        """执行文件系统操作，不阻塞事件循环。"""
        self._directory.mkdir(parents=True, exist_ok=True)
        handle = (self._directory / "instance.lock").open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ConflictError("该数据目录已有运行中的单实例") from exc
        self._file = handle

    async def acquire(self) -> None:
        """同一个守卫重复启动不重复占用文件句柄。"""
        if self._file is None:
            work = asyncio.create_task(asyncio.to_thread(self._acquire))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # 文件操作不能在线程中被取消；等它结束后回收句柄再传播取消。
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                try:
                    work.result()
                finally:
                    await self.close()
                raise

    async def close(self) -> None:
        """关闭句柄即释放锁，保留稳定锁文件路径。"""
        handle, self._file = self._file, None
        if handle is not None:
            handle.close()


class PostgresInstanceGuard:
    """不占用业务连接池，数据库锁连接终止即拒绝新工作。"""

    def __init__(self, connect: Callable[[], Awaitable[asyncpg.Connection]], scope: str) -> None:
        """连接工厂由装配入口注入，scope 指向控制面命名空间。"""
        self._connect = connect
        self._scope = scope
        self._connection: asyncpg.Connection | None = None
        self._closing = False
        self._lost = False
        self.on_lost: Callable[[], None] | None = None

    @property
    def lost(self) -> bool:
        """显式丢锁标记不能被自动重连清除。"""
        return self._lost

    def _terminated(self, connection: object) -> None:
        """连接终止回调同步关闭接收闸门，清理可异步执行。"""
        if not self._closing and connection is self._connection:
            self._lost = True
            if self.on_lost is not None:
                self.on_lost()

    async def acquire(self) -> None:
        """按数据库与命名空间竞争会话级咨询锁。"""
        if self.lost or self._closing:
            raise ConflictError("单实例守卫已关闭或持有权已丢失，必须重新创建并启动")
        if self._connection is not None:
            return
        connection = await self._connect()
        try:
            database = await connection.fetchval("SELECT current_database()")
            digest = hashlib.sha256(f"xuanpu:instance:{database}:{self._scope}".encode()).digest()
            key = int.from_bytes(digest[:8], "big", signed=True)
            if not await connection.fetchval("SELECT pg_try_advisory_lock($1)", key):
                raise ConflictError("该控制面已有运行中的单实例")
            self._connection = connection
            connection.add_termination_listener(self._terminated)
        except BaseException:
            await connection.close()
            raise

    async def close(self) -> None:
        """正常关闭不触发异常丢锁；关闭专用连接释放会话锁。"""
        self._closing = True
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()
