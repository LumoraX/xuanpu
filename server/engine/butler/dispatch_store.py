"""派工记录存储契约：memory 与 PostgreSQL 实现同一套行为，测试共用。

- 按租户与员工隔离；归属不符与不存在一律 ``NotFoundError``。
- 建记录幂等：同一员工同一 ``request_id``，指纹相同回原记录，不同报冲突。
- 每条 trace 的匠人数与租户进行中 trace 数在建记录的同一临界区内核对，超限抛
  ``ButlerBudgetError`` 且不建记录（并发派工不能一起越过上限）。
- 改状态必须带期望版本，版本不符报冲突。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from server.core.errors import ConflictError
from server.engine.butler.budget import ButlerBudgetError

if TYPE_CHECKING:
    from datetime import datetime

    from server.engine.butler.models import DispatchRecord, DispatchState
    from server.engine.tracing.models import TraceOwner


class DispatchStore(Protocol):
    """派工记录存储。"""

    async def create(
        self, owner: TraceOwner, record: DispatchRecord, *,
        max_per_trace: int, max_active_traces: int,
    ) -> tuple[DispatchRecord, bool]:
        """建派工或回放原记录；返回 ``(记录, 是否新建)``。"""
        ...

    async def get(self, owner: TraceOwner, dispatch_id: str) -> DispatchRecord:
        """读一条本人的派工。"""
        ...

    async def list_for_trace(self, owner: TraceOwner, trace_id: str) -> tuple[DispatchRecord, ...]:
        """一条 trace 下本人的全部派工，按创建先后。"""
        ...

    async def list_recent(self, owner: TraceOwner, *, limit: int = 20) -> tuple[DispatchRecord, ...]:
        """本人最近的派工记录，按创建时间倒序。"""
        ...

    async def update(
        self, owner: TraceOwner, dispatch_id: str, *, expected_version: int,
        to: DispatchState, at: datetime, host_ref: str | None = None,
        end_reason: str | None = None,
    ) -> DispatchRecord:
        """按状态机推进一步。"""
        ...

    async def active_traces(self, tenant_id: str) -> int:
        """租户当前有进行中派工的 trace 数。"""
        ...

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[DispatchRecord, ...]:
        """租户内进行中的派工（供对账器），按创建先后。"""
        ...

    async def find_by_host(self, owner: TraceOwner, host_ref: str) -> DispatchRecord | None:
        """本人承载在 ``host_ref``（会话或任务）上的派工；没有返回 None。"""
        ...


def check_owner(owner: TraceOwner, record: DispatchRecord) -> None:
    """建记录时归属必须与调用方一致。"""
    if (record.tenant_id, record.user_id) != (owner.tenant_id, owner.user_id):
        raise ConflictError("派工归属与调用方不符")


def replay(previous: DispatchRecord, record: DispatchRecord) -> DispatchRecord:
    """同键同体回原记录，不同体冲突。"""
    if previous.input_fingerprint != record.input_fingerprint:
        raise ConflictError("同一请求标识对应的派工内容不同")
    return previous


def check_capacity(
    *, in_trace: int, trace_active: bool, active_traces: int,
    max_per_trace: int, max_active_traces: int,
) -> None:
    """建记录前的容量核对（在存储的临界区内调用）。"""
    if in_trace >= max_per_trace:
        raise ButlerBudgetError("artisans_per_trace", "本次请求可派出的匠人数已达上限")
    if not trace_active and active_traces >= max_active_traces:
        raise ButlerBudgetError("active_traces_per_tenant", "企业当前进行中的派工过多，请稍后再试")
