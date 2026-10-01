"""会话租约：本实例对会话的持有权。

一个会话同一时刻只由一个实例持有（单写者）。持有权是带期限的租约，记在控制面里：
持有者定期续期；续期失败说明持有权已经丢了，调用方要立即停写、放手。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.controlplane.store import ControlPlaneStore

logger = logging.getLogger(__name__)

SessionKey = tuple[str, str]


class SessionLeases:
    """本实例的会话租约：取得、确认、续期、放弃。"""

    def __init__(
        self,
        store: ControlPlaneStore,
        *,
        instance_id: str,
        lease_seconds: float,
        now: Callable[[], float],
    ) -> None:
        """
        Args:
            store: 控制面存储，租约记在会话记录上。
            instance_id: 本实例的标识，写进租约。
            lease_seconds: 租约时长。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._owner = instance_id
        self._lease_seconds = lease_seconds
        self._now = now

    @property
    def lease_seconds(self) -> float:
        """租约时长。"""
        return self._lease_seconds

    async def _acquire(self, tenant_id: str, session_id: str) -> SessionRecord:
        """取得或续期；被别的实例持有时抛 ``SessionBusyError``。"""
        return await self._store.acquire_lease(
            tenant_id,
            session_id,
            owner=self._owner,
            now=self._now(),
            ttl_seconds=self._lease_seconds,
        )

    async def hold(self, record: SessionRecord) -> SessionRecord:
        """取得会话的持有权，返回取得之后的最新会话记录。"""
        return await self._acquire(record.tenant_id, record.session_id)

    async def confirm(self, previous: SessionRecord) -> SessionRecord:
        """再次确认仍持有会话（准备引擎这类没人续期的长操作之后），返回最新会话记录。

        Args:
            previous: 上一次 ``hold`` 或 ``confirm`` 返回的记录。
        """
        return await self._acquire(previous.tenant_id, previous.session_id)

    async def release(self, record: SessionRecord) -> bool:
        """放弃持有权；本来就不归本实例时什么也不改，返回 False。"""
        return await self._store.release_lease(
            record.tenant_id, record.session_id, owner=self._owner
        )

    async def renew(self, keys: Iterable[SessionKey]) -> list[SessionKey]:
        """给这些会话续期，返回丢了持有权的那些（逐个记日志）。"""
        lost: list[SessionKey] = []
        for tenant_id, session_id in keys:
            try:
                await self._acquire(tenant_id, session_id)
            except Exception:
                logger.exception("会话 %s 续期失败，本实例放弃持有", session_id)
                lost.append((tenant_id, session_id))
        return lost


__all__ = ["SessionKey", "SessionLeases"]
