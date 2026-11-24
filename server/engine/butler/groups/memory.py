"""编组记录的内存实现：单进程、一把锁，行为与 PostgreSQL 实现一致。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from server.core.errors import ConflictError, NotFoundError
from server.engine.butler.groups.store import bumped, check_owner, replay

if TYPE_CHECKING:
    from server.engine.butler.groups.plan import GroupRecord
    from server.engine.tracing.models import TraceOwner


class MemoryGroupStore:
    """按 ``(租户, 编组)`` 存记录。"""

    def __init__(self) -> None:
        """空存储。"""
        self._records: dict[tuple[str, str], GroupRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, owner: TraceOwner, record: GroupRecord) -> tuple[GroupRecord, bool]:
        """先查幂等再写入。"""
        check_owner(owner, record)
        async with self._lock:
            for existing in self._records.values():
                if (existing.tenant_id, existing.user_id, existing.request_id) == (
                        record.tenant_id, record.user_id, record.request_id):
                    return replay(existing, record), False
            if (record.tenant_id, record.group_id) in self._records:
                raise ConflictError("编组标识已被占用")
            self._records[(record.tenant_id, record.group_id)] = record
            return record, True

    async def get(self, owner: TraceOwner, group_id: str) -> GroupRecord:
        """他人的与不存在的一样 404。"""
        record = self._records.get((owner.tenant_id, group_id))
        if record is None or record.user_id != owner.user_id:
            raise NotFoundError("编组不存在")
        return record

    async def save(self, owner: TraceOwner, record: GroupRecord, *,
                   expected_version: int) -> GroupRecord:
        """版本相符才保存。"""
        check_owner(owner, record)
        async with self._lock:
            current = await self.get(owner, record.group_id)
            saved = bumped(record, current.version, expected_version)
            self._records[(owner.tenant_id, record.group_id)] = saved
            return saved

    async def find_by_request(self, owner: TraceOwner, request_id: str) -> GroupRecord | None:
        """本人按请求标识建过的编组。"""
        for record in self._records.values():
            if (record.tenant_id, record.user_id, record.request_id) == (
                    owner.tenant_id, owner.user_id, request_id):
                return record
        return None

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[GroupRecord, ...]:
        """还没收口的编组。"""
        found = [r for r in self._records.values() if r.tenant_id == tenant_id and r.active]
        return tuple(sorted(found, key=lambda r: (r.created_at, r.group_id))[:limit])
