"""会话租约：本实例对会话的持有权。

一个会话同一时刻只由一个实例持有（单写者）。持有权是带期限的租约，记在控制面里：
持有者定期续期；续期失败说明持有权已经丢了，调用方要立即停写、放手。

本实例记着每个会话上一次取得租约时的到期时刻。再次确认或续期时已经过了这个时刻，
就算这一次又拿到了租约，中间也断过——别的实例可能接手、写过、又放了手，本实例手里
引擎的历史不再可信，按丢失处理。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from xuanpu_server.errors import LeaseInterruptedError, SessionBusyError

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
        self._expires: dict[SessionKey, float] = {}

    @property
    def lease_seconds(self) -> float:
        """租约时长。"""
        return self._lease_seconds

    async def _acquire(self, key: SessionKey, now: float) -> SessionRecord:
        """取得或续期，并记下新的到期时刻；被别的实例持有时抛 ``SessionBusyError``。"""
        fresh = await self._store.acquire_lease(
            key[0], key[1], owner=self._owner, now=now, ttl_seconds=self._lease_seconds
        )
        self._expires[key] = fresh.lease_expires_at
        return fresh

    def _lapsed(self, key: SessionKey, now: float) -> bool:
        """上一次取得的租约在 ``now`` 之前就到期了（或根本没取得过）。"""
        expires = self._expires.get(key)
        return expires is None or now >= expires

    async def hold(self, record: SessionRecord) -> SessionRecord:
        """取得会话的持有权，返回取得之后的最新会话记录。"""
        return await self._acquire((record.tenant_id, record.session_id), self._now())

    async def confirm(self, previous: SessionRecord) -> SessionRecord:
        """再次确认仍持有会话（准备引擎这类没人续期的长操作之后），返回最新会话记录。

        先续期：别的实例正持有时抛 ``SessionBusyError``（带持有者）。续上了、但上一次的
        租约在此之前已经到期，说明持有中断过，抛 ``LeaseInterruptedError``；这时租约在
        本实例手里，调用方收尾时照常放弃。

        Args:
            previous: 上一次 ``hold`` 或 ``confirm`` 返回的记录。
        """
        key = (previous.tenant_id, previous.session_id)
        now = self._now()
        lapsed = self._lapsed(key, now)
        fresh = await self._acquire(key, now)
        if lapsed:
            raise LeaseInterruptedError(
                f"会话 {previous.session_id!r} 的持有在准备期间中断过，请重试"
            )
        return fresh

    def ensure_not_held_elsewhere(self, record: SessionRecord) -> None:
        """只看记录、不碰租约：会话正由别的实例持有（租约未到期）时抛 ``SessionBusyError``。"""
        owner = record.owner
        if owner is not None and owner != self._owner and record.lease_expires_at > self._now():
            raise SessionBusyError(f"会话 {record.session_id!r} 正由另一个实例持有", owner=owner)

    async def release(self, record: SessionRecord) -> bool:
        """放弃持有权；本来就不归本实例时什么也不改，返回 False。"""
        self._expires.pop((record.tenant_id, record.session_id), None)
        return await self._store.release_lease(
            record.tenant_id, record.session_id, owner=self._owner
        )

    async def renew(self, keys: Iterable[SessionKey]) -> list[SessionKey]:
        """给这些会话续期，返回丢了持有权的那些（逐个记日志）。

        上一次的租约已经到期的不再续期，直接算丢失：中间可能有人接手写过。
        """
        lost: list[SessionKey] = []
        for key in keys:
            now = self._now()
            if self._lapsed(key, now):
                logger.warning("会话 %s 的租约到期之后才轮到续期，本实例放弃持有", key[1])
                lost.append(key)
                continue
            try:
                await self._acquire(key, now)
            except Exception:
                logger.exception("会话 %s 续期失败，本实例放弃持有", key[1])
                lost.append(key)
        return lost


__all__ = ["SessionKey", "SessionLeases"]
