"""同一PG员工事务内复用只读查询，避免连接池耗尽后的锁等待死环。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class TransactionReadContext:
    """连接只属于同一协程；继承上下文的后台任务不能复用它。"""

    pool: Any
    connection: Any
    task: asyncio.Task[Any] | None


_current: ContextVar[TransactionReadContext | None] = ContextVar('pg_transaction_reads', default=None)


@contextmanager
def transaction_reads(pool: Any, connection: Any) -> Iterator[None]:
    """只在已有事务yield区间绑定，不创建后台查询或隐式写事务。"""
    token = _current.set(TransactionReadContext(pool,connection,asyncio.current_task()))
    try:
        yield
    finally:
        _current.reset(token)


def read_connection(pool: Any, sql: str) -> Any:
    """仅相同池、相同协程的SELECT可复用连接；写用例保持原事务边界。"""
    current = _current.get()
    if (current is not None and current.pool is pool and current.task is asyncio.current_task()
            and sql.lstrip().upper().startswith('SELECT ')):
        return current.connection
    return None
