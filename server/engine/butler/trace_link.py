"""派工与执行追踪的关联：派工 span 的写入，以及匠人引擎根执行的绑定查询。

- 派工 span ``dp.<dispatch_id>``（kind ``dispatch``）挂在管家的根 span 下，作为一个独立写入方
  ``dp.<dispatch_id>`` 写进管家的 trace，序号就是派工记录的版本（每次状态变化加一）。
  所有派工的建与改都经 ``TracedDispatchStore``，版本连续；某次写入失败则下一次跳号，trace
  如实标不完整，不回填。
- 匠人引擎的根执行据持久记录绑定到派工 span 下：会话承载按派工记录的 ``host_ref``（会话在发出
  任务说明、引擎创建之前就已写入）；任务承载按任务的 ``request_id``（``butler:<dispatch_id>``）。
  查不到的会话（旧工作、非派工）照旧单独成 trace，不猜父级。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from server.core.errors import NotFoundError
from server.core.tenancy import Principal
from server.engine.tracing.binding import TraceBinding
from server.engine.tracing.models import SpanFact

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from server.core.controlplane.store import ControlPlaneStore
    from server.domains.work_tasks.store import WorkTaskStore
    from server.engine.butler.dispatch_store import DispatchStore
    from server.engine.butler.models import DispatchRecord, DispatchState
    from server.engine.tracing.config import ExecutionTraceConfig
    from server.engine.tracing.models import SpanState, TraceOwner
    from server.engine.tracing.store import SpanStore

logger = logging.getLogger(__name__)
REQUEST_PREFIX = "butler:"
_STATES: dict[str, SpanState | None] = {
    "accepted": "queued", "running": "running", "asking": "waiting_input",
    "completed": "completed", "failed": "failed", "cancelled": "cancelled", "delivered": None,
}


def dispatch_span(dispatch_id: str) -> str:
    """派工 span 的标识，同时是它在 trace 里的写入方名。"""
    return f"dp.{dispatch_id}"


def dispatch_fact(record: DispatchRecord) -> SpanFact:
    """派工记录当前状态对应的一条事实；序号是记录版本。"""
    return SpanFact(
        seq=record.version, span_id=dispatch_span(record.dispatch_id), at=record.updated_at,
        kind="dispatch", name=f"{record.artisan.package}/{record.artisan.entry}",
        parent_span_id=record.parent_span_id, ref=record.dispatch_id,
        state=_STATES[record.state], end_reason=record.end_reason,
    )


class TracedDispatchStore:
    """包一层派工存储：每次建与改成功后把派工 span 写进管家的 trace。

    追踪写入失败只记日志、尽力标不完整，不影响派工本身。
    """

    def __init__(
        self, inner: DispatchStore, traces: SpanStore,
        config: Callable[[], ExecutionTraceConfig],
    ) -> None:
        """依赖由平台装配注入；追踪配置每次现取（运行时默认参数可以整体替换）。"""
        self._inner, self._traces, self._config = inner, traces, config

    async def create(
        self, owner: TraceOwner, record: DispatchRecord, *,
        max_per_trace: int, max_active_traces: int,
    ) -> tuple[DispatchRecord, bool]:
        """建记录；新建时写派工 span。"""
        created, fresh = await self._inner.create(owner, record, max_per_trace=max_per_trace,
                                                  max_active_traces=max_active_traces)
        if fresh:
            await self._record(owner, created)
        return created, fresh

    async def get(self, owner: TraceOwner, dispatch_id: str) -> DispatchRecord:
        """透传。"""
        return await self._inner.get(owner, dispatch_id)

    async def list_for_trace(self, owner: TraceOwner, trace_id: str) -> tuple[DispatchRecord, ...]:
        """透传。"""
        return await self._inner.list_for_trace(owner, trace_id)

    async def update(
        self, owner: TraceOwner, dispatch_id: str, *, expected_version: int,
        to: DispatchState, at: datetime, host_ref: str | None = None,
        end_reason: str | None = None,
    ) -> DispatchRecord:
        """推进状态后写派工 span。"""
        updated = await self._inner.update(owner, dispatch_id, expected_version=expected_version,
                                           to=to, at=at, host_ref=host_ref, end_reason=end_reason)
        await self._record(owner, updated)
        return updated

    async def active_traces(self, tenant_id: str) -> int:
        """透传。"""
        return await self._inner.active_traces(tenant_id)

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[DispatchRecord, ...]:
        """透传。"""
        return await self._inner.active(tenant_id, limit=limit)

    async def find_by_host(self, owner: TraceOwner, host_ref: str) -> DispatchRecord | None:
        """透传。"""
        return await self._inner.find_by_host(owner, host_ref)

    async def _record(self, owner: TraceOwner, record: DispatchRecord) -> None:
        """确保 trace 存在后写一条派工事实；失败尽力标不完整。"""
        config = self._config()
        if not config.allows(owner.tenant_id):
            return
        try:
            await self._traces.open_trace(owner, trace_id=record.trace_id, origin="user",
                                          title="管家", at=record.created_at)
            await self._traces.apply(owner, record.trace_id, [dispatch_fact(record)],
                                     source=dispatch_span(record.dispatch_id),
                                     max_spans=config.max_spans_per_trace,
                                     max_attempts=config.max_attempts_per_span)
        except Exception:
            logger.exception("派工 %s 写入执行追踪失败", record.dispatch_id)
            try:
                await self._traces.mark_incomplete(owner, record.trace_id)
            except Exception:
                logger.exception("派工 %s 的 trace 标不完整也失败", record.dispatch_id)


class DispatchTraceBinder:
    """按会话查派工绑定：会话承载看 ``host_ref``，任务承载看任务的请求标识。"""

    def __init__(self, control: ControlPlaneStore, tasks: WorkTaskStore) -> None:
        """依赖由平台装配注入。"""
        self._control, self._tasks = control, tasks

    async def bind(self, owner: TraceOwner, session_id: str) -> TraceBinding | None:
        """查不到派工返回 None；存储错误照常抛出。"""
        dispatches = self._control.dispatch_store
        record = await dispatches.find_by_host(owner, session_id)
        if record is None:
            session = await self._control.get_session(owner.tenant_id, session_id)
            task_id = None if session is None else session.work_task_id
            if task_id is None:
                return None
            record = await self._from_task(owner, task_id)
        if record is None:
            return None
        return TraceBinding(trace_id=record.trace_id,
                            parent_span_id=dispatch_span(record.dispatch_id))

    async def _from_task(self, owner: TraceOwner, task_id: str) -> DispatchRecord | None:
        """任务的请求标识形如 ``butler:<dispatch_id>`` 时取该派工。"""
        who = Principal(role="tenant_member", tenant_id=owner.tenant_id, user_id=owner.user_id,
                        key_id=None)
        task = await self._tasks.get(who, task_id)
        if not task.request_id.startswith(REQUEST_PREFIX):
            return None
        try:
            return await self._control.dispatch_store.get(
                owner, task.request_id.removeprefix(REQUEST_PREFIX))
        except NotFoundError:
            logger.warning("任务 %s 指向的派工不存在，按未绑定处理", task_id)
            return None
