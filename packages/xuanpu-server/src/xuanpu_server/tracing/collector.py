"""一个引擎的 span 采集器：同步登记事实，后台批量写入；任何失败都只降为“记录不完整”。

trace 的边界：根执行开始一轮（``turn_started`` 且没有父执行）时打开一条 trace；同一根执行
挂起后续跑仍记在这条 trace 上，根执行到达完成、失败或取消时这条 trace 结束，下一轮另开。
子执行沿父执行归到同一条 trace。父执行没见过（晚订阅、冷恢复）的执行单独成 trace 并标不完整。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 —— 数据类字段
from typing import TYPE_CHECKING

from xuanpu_server.ids import new_id
from xuanpu_server.tracing.events import drafts, event_time
from xuanpu_server.tracing.models import SpanFact, TraceOwner

if TYPE_CHECKING:
    import taifeng

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.tracing.config import ExecutionTraceConfig
    from xuanpu_server.tracing.events import Draft
    from xuanpu_server.tracing.models import SpanKind, TraceOrigin
    from xuanpu_server.tracing.store import SpanStore

logger = logging.getLogger(__name__)
_FLUSH_SECONDS = 0.5
_MAX_PENDING = 10_000
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_ENDED_MEMORY = 1024


@dataclass
class _Trace:
    """一条正在采集的 trace：待写草稿与下一个序号。``next_seq`` 为 None 表示还没建。"""

    trace_id: str
    title: str
    opened_at: datetime
    pending: list[Draft] = field(default_factory=list)
    next_seq: int | None = None
    orphan: bool = False
    dropped: bool = False


class TraceCollector:
    """接在 ``EngineEventHub`` 上；``handle`` 同步且不抛异常，写入在后台任务里做。"""

    def __init__(
        self, store: SpanStore, owner: TraceOwner, config: ExecutionTraceConfig, *,
        ref: str | None, origin: TraceOrigin = "user", root_kind: SpanKind = "artisan",
    ) -> None:
        """
        Args:
            store: span 存储。
            owner: 会话主人；trace 归属于他。
            config: 上限配置。
            ref: 根执行 span 指向的引用（会话标识）。
            origin: trace 的来源。
            root_kind: 根执行 span 的类型（业务会话是 artisan，管家会话是 butler）。
        """
        self._store, self._owner, self._config = store, owner, config
        self._ref, self._origin, self._root_kind = ref, origin, root_kind
        self._traces: dict[str, _Trace] = {}
        self._by_execution: dict[str, str] = {}
        self._finished: list[str] = []
        # 已结束的根执行 → 结束时的尝试；同一尝试的迟到事件丢弃，新尝试另开 trace
        self._ended: dict[str, str] = {}
        self._pending_total = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动后台写入。"""
        self._task = asyncio.ensure_future(self._loop())

    def handle(self, event: taifeng.EventMsg) -> None:
        """登记一条事件产生的事实。不让出、不抛异常：采集不能拖慢或打断对话。"""
        try:
            self._handle(event)
        except Exception:
            logger.exception("执行追踪登记事件失败")

    async def flush(self) -> None:
        """把待写事实写出去。失败只记日志并在之后以跳号体现为不完整。"""
        async with self._lock:
            for trace in list(self._traces.values()):
                await self._write(trace)
            for trace_id in self._finished:
                done = self._traces.get(trace_id)
                # 有待补的“不完整”标记时先留着，下一次写出后再移除
                if done is not None and not done.pending and not done.dropped:
                    del self._traces[trace_id]
            self._finished = [t for t in self._finished if t in self._traces]

    async def close(self) -> None:
        """停后台任务并写完；仍未结束的 trace 标为不完整（引擎在执行中途被关闭）。"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.flush()
        for trace in self._traces.values():
            if trace.next_seq is not None:
                with contextlib.suppress(Exception):
                    await self._store.mark_incomplete(self._owner, trace.trace_id)
        self._traces.clear()

    def _handle(self, event: taifeng.EventMsg) -> None:
        """找到所属 trace，追加草稿；根执行结束时登记 trace 结束。"""
        identity = event.execution
        if identity is None or self._ended.get(identity.execution_id) == identity.attempt_id:
            return
        trace = self._trace_for(event)
        if trace is None:
            return
        found = drafts(event, root_kind=self._root_kind, ref=self._ref)
        if self._pending_total + len(found) > _MAX_PENDING:
            trace.dropped = True
            return
        trace.pending.extend(found)
        self._pending_total += len(found)
        is_root = identity.parent_execution_id is None
        ended = event.msg.kind in ("turn_completed", "turn_failed") and any(
            d.state in _TERMINAL for d in found)
        if is_root and ended:
            self._by_execution = {k: v for k, v in self._by_execution.items()
                                  if v != trace.trace_id}
            self._finished.append(trace.trace_id)
            self._ended[identity.execution_id] = identity.attempt_id
            if len(self._ended) > _ENDED_MEMORY:
                self._ended.pop(next(iter(self._ended)))

    def _trace_for(self, event: taifeng.EventMsg) -> _Trace | None:
        """按执行身份找 trace；需要时新开。"""
        identity = event.execution
        assert identity is not None
        known = self._by_execution.get(identity.execution_id)
        if known is not None:
            return self._traces.get(known)
        parent = identity.parent_execution_id
        if parent is not None and parent in self._by_execution:
            trace_id = self._by_execution[parent]
            self._by_execution[identity.execution_id] = trace_id
            return self._traces.get(trace_id)
        # 没见过开头或父执行的执行（晚订阅、冷恢复后续跑）：仍然记下，但标不完整
        trace = _Trace(trace_id=new_id("trc"), title=identity.skill_id, opened_at=event_time(event),
                       orphan=parent is not None or event.msg.kind != "turn_started")
        self._traces[trace.trace_id] = trace
        self._by_execution[identity.execution_id] = trace.trace_id
        return trace

    async def _write(self, trace: _Trace) -> None:
        """先确保 trace 已建，再按序号写一批。失败的批次不回填，只让 trace 标为不完整。"""
        if not trace.pending and not trace.dropped:
            return
        batch, trace.pending = trace.pending, []
        self._pending_total -= len(batch)
        try:
            if trace.next_seq is None:
                record = await self._store.open_trace(
                    self._owner, trace_id=trace.trace_id, origin=self._origin,
                    title=trace.title, at=trace.opened_at,
                )
                trace.next_seq = record.last_seq + 1
            facts = self._numbered(trace, batch)
            if trace.dropped or trace.orphan:
                await self._store.mark_incomplete(self._owner, trace.trace_id)
                trace.dropped = trace.orphan = False
            if facts:
                await self._store.apply(
                    self._owner, trace.trace_id, facts,
                    max_spans=self._config.max_spans_per_trace,
                    max_attempts=self._config.max_attempts_per_span,
                )
        except Exception:
            trace.dropped = True
            logger.exception("执行追踪写入失败（trace %s，%d 条事实）", trace.trace_id, len(batch))

    def _numbered(self, trace: _Trace, batch: list[Draft]) -> list[SpanFact]:
        """逐条编号；构造不出合法事实的草稿跳过并标不完整（序号照样前进）。"""
        assert trace.next_seq is not None
        facts = []
        for draft in batch:
            try:
                facts.append(SpanFact(seq=trace.next_seq, **draft.fields()))
            except ValueError:
                trace.dropped = True
            trace.next_seq += 1
        return facts

    async def _loop(self) -> None:
        """定时写出。"""
        while True:
            await asyncio.sleep(_FLUSH_SECONDS)
            await self.flush()


def tracer_for(
    store: ControlPlaneStore, config: ExecutionTraceConfig | None,
    tenant_id: str, session_id: str, user_id: str | None,
) -> TraceCollector | None:
    """租户开启追踪且会话有主人时，给这个引擎配一个采集器；否则不采集。"""
    if config is None or user_id is None or not config.allows(tenant_id):
        return None
    owner = TraceOwner(tenant_id=tenant_id, user_id=user_id)
    return TraceCollector(store.trace_store, owner, config, ref=session_id)
