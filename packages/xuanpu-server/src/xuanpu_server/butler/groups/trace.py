"""编组 span：``gp.<group_id>``（kind ``group``）挂在管家根 span 下，成员派工 span 挂在它下面。

``TracedGroupStore`` 每次建与保存后写一条事实，写入方名即 span 标识，序号是编组版本；
写入失败只记日志、尽力标不完整，不影响编组本身。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from xuanpu_server.tracing.models import SpanFact

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.butler.groups.plan import GroupRecord
    from xuanpu_server.butler.groups.store import GroupStore
    from xuanpu_server.tracing.config import ExecutionTraceConfig
    from xuanpu_server.tracing.models import SpanState, TraceOwner
    from xuanpu_server.tracing.store import SpanStore

logger = logging.getLogger(__name__)


def group_span(group_id: str) -> str:
    """编组 span 的标识，同时是它在 trace 里的写入方名。"""
    return f"gp.{group_id}"


def group_fact(record: GroupRecord) -> SpanFact:
    """编组当前状态对应的一条事实。"""
    state: SpanState = "running" if record.state == "running" else record.state
    return SpanFact(seq=record.version, span_id=group_span(record.group_id), at=record.updated_at,
                    kind="group", name=record.plan.title, parent_span_id=record.parent_span_id,
                    ref=record.group_id, state=state)


class TracedGroupStore:
    """包一层编组存储：建与保存成功后写编组 span。"""

    def __init__(self, inner: GroupStore, traces: SpanStore,
                 config: Callable[[], ExecutionTraceConfig]) -> None:
        """依赖由平台装配注入；追踪配置每次现取。"""
        self._inner, self._traces, self._config = inner, traces, config

    async def create(self, owner: TraceOwner, record: GroupRecord) -> tuple[GroupRecord, bool]:
        """新建时写编组 span。"""
        created, fresh = await self._inner.create(owner, record)
        if fresh:
            await self._record(owner, created)
        return created, fresh

    async def get(self, owner: TraceOwner, group_id: str) -> GroupRecord:
        """透传。"""
        return await self._inner.get(owner, group_id)

    async def save(self, owner: TraceOwner, record: GroupRecord, *,
                   expected_version: int) -> GroupRecord:
        """保存后写编组 span。"""
        saved = await self._inner.save(owner, record, expected_version=expected_version)
        await self._record(owner, saved)
        return saved

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[GroupRecord, ...]:
        """透传。"""
        return await self._inner.active(tenant_id, limit=limit)

    async def find_by_request(self, owner: TraceOwner, request_id: str) -> GroupRecord | None:
        """透传。"""
        return await self._inner.find_by_request(owner, request_id)

    async def _record(self, owner: TraceOwner, record: GroupRecord) -> None:
        """确保 trace 存在后写一条编组事实；失败尽力标不完整。"""
        config = self._config()
        if not config.allows(owner.tenant_id):
            return
        try:
            await self._traces.open_trace(owner, trace_id=record.trace_id, origin="user",
                                          title="管家", at=record.created_at)
            await self._traces.apply(owner, record.trace_id, [group_fact(record)],
                                     source=group_span(record.group_id),
                                     max_spans=config.max_spans_per_trace,
                                     max_attempts=config.max_attempts_per_span)
        except Exception:
            logger.exception("编组 %s 写入执行追踪失败", record.group_id)
            try:
                await self._traces.mark_incomplete(owner, record.trace_id)
            except Exception:
                logger.exception("编组 %s 的 trace 标不完整也失败", record.group_id)
