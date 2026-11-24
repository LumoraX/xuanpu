"""编组存储契约：memory 与 PostgreSQL 实现同一套行为。

- 按租户与员工隔离；归属不符与不存在一律 ``NotFoundError``。
- 建记录幂等：同一员工同一 ``request_id``，指纹相同回原记录，不同报冲突。
- 保存整条记录须带期望版本，版本不符报冲突；保存时版本加一。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from server.core.errors import ConflictError

if TYPE_CHECKING:
    from server.engine.butler.groups.plan import GroupRecord
    from server.engine.tracing.models import TraceOwner


class GroupStore(Protocol):
    """编组记录存储。"""

    async def create(self, owner: TraceOwner, record: GroupRecord) -> tuple[GroupRecord, bool]:
        """建编组或回放原记录；返回 ``(记录, 是否新建)``。"""
        ...

    async def get(self, owner: TraceOwner, group_id: str) -> GroupRecord:
        """读一个本人的编组。"""
        ...

    async def save(self, owner: TraceOwner, record: GroupRecord, *,
                   expected_version: int) -> GroupRecord:
        """整体保存；返回版本加一后的记录。"""
        ...

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[GroupRecord, ...]:
        """租户内还没收口的编组，按创建先后。"""
        ...

    async def find_by_request(self, owner: TraceOwner, request_id: str) -> GroupRecord | None:
        """本人按请求标识建过的编组；没有返回 None。"""
        ...


def check_owner(owner: TraceOwner, record: GroupRecord) -> None:
    """记录归属必须与调用方一致。"""
    if (record.tenant_id, record.user_id) != (owner.tenant_id, owner.user_id):
        raise ConflictError("编组归属与调用方不符")


def replay(previous: GroupRecord, record: GroupRecord) -> GroupRecord:
    """同键同体回原记录，不同体冲突。"""
    if previous.input_fingerprint != record.input_fingerprint:
        raise ConflictError("同一请求标识对应的编组内容不同")
    return previous


def bumped(record: GroupRecord, current_version: int, expected_version: int) -> GroupRecord:
    """核对版本并返回版本加一的记录。"""
    if current_version != expected_version or record.version != expected_version:
        raise ConflictError("编组已被更新，请重新读取")
    return record.model_copy(update={"version": expected_version + 1})
