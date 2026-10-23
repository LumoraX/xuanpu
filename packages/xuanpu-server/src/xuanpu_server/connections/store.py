"""连接域的短租户事务协议，网络请求必须在事务外。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager


@dataclass
class ConnectionState:
    """内部状态快照；密文独立持久化且永不进入公共视图。"""
    records: dict[str,dict[str,Any]] = field(default_factory=dict)
    credentials: dict[str,bytes] = field(default_factory=dict)
    audits: list[dict[str,Any]] = field(default_factory=list)


class ConnectionStore(Protocol):
    """同租户串行、失败回滚，跨租户键隔离。"""
    def transaction(self,tenant: str) -> AbstractAsyncContextManager[ConnectionState]:
        """进入短事务，不在锁内等待供应商。"""
        ...

    async def read_record(self, tenant: str, identifier: str) -> dict[str, Any] | None:
        """协调事务内读取安装元数据，PG复用同连接且不嵌套连接事务。"""
        ...

    async def put_attempt(self,state_hash: str,record: dict[str,Any],*,now: float) -> None:
        """保存五分钟登录尝试，清理过期记录并限制单租户并发量。"""
        ...

    async def take_attempt(self,state_hash: str,*,now: float) -> dict[str,Any] | None:
        """一次取出即删；过期记录仍消费但不返回。"""
        ...
