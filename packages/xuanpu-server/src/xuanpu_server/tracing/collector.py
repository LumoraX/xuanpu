"""一个引擎的 span 采集器：同步登记事实，后台批量写入；任何失败都只降为“记录不完整”。

trace 的边界：根执行开始一轮（``turn_started`` 且没有父执行）时打开一条 trace；同一根执行
挂起后续跑仍记在这条 trace 上，根执行到达完成、失败或取消时这条 trace 结束，下一轮另开。
子执行沿父执行归到同一条 trace。父执行没见过（晚订阅、冷恢复）的执行单独成 trace 并标不完整。

跨引擎：给了绑定查询（``binder``）时，从头开始的根 trace 在第一次写出前查一次持久绑定；
查到则写进绑定的 trace，根执行 span 挂到绑定的父 span 下（例如匠人会话挂到管家的派工 span）。
每条本地 trace 是 trace 里的一个独立写入方，序号各自连续，与其他引擎的写入互不干扰。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 —— 数据类字段
from typing import TYPE_CHECKING

from xuanpu_server.ids import new_id
from xuanpu_server.tracing.events import drafts, event_time
from xuanpu_server.tracing.models import SpanFact, TraceOwner
from xuanpu_server.tracing.narration import append, narration

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import taifeng

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.tracing.binding import TraceBinding
    from xuanpu_server.tracing.config import ExecutionTraceConfig, TraceWiring
    from xuanpu_server.tracing.events import Draft
    from xuanpu_server.tracing.models import SpanKind, TraceOrigin
    from xuanpu_server.tracing.store import SpanStore

logger = logging.getLogger(__name__)
_FLUSH_SECONDS = 0.5
_MAX_PENDING = 10_000
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_ENDED_MEMORY = 1024
_TURN_ENDS = frozenset({"turn_completed", "turn_failed", "turn_suspended"})
# 旁白缓冲最多记这么多个执行；执行没见到结束事件（取消、崩溃）时不会无限增长
_SAID_MEMORY = 1024


@dataclass
class _Trace:
    """一条正在采集的 trace：待写草稿与下一个序号。``next_seq`` 为 None 表示还没建。

    ``key`` 是本采集器内的标识，也是它在 trace 里的写入方名；``trace_id`` 可能在第一次
    写出前因绑定而改为别处的 trace。
    """

    key: str
    trace_id: str
    title: str
    opened_at: datetime
    pending: list[Draft] = field(default_factory=list)
    next_seq: int | None = None
    orphan: bool = False
    dropped: bool = False
    bindable: bool = False
    parent_span: str | None = None


class TraceCollector:
    """接在 ``EngineEventHub`` 上；``handle`` 同步且不抛异常，写入在后台任务里做。"""

    def __init__(
        self, store: SpanStore, owner: TraceOwner, config: ExecutionTraceConfig, *,
        ref: str | None, origin: TraceOrigin = "user", root_kind: SpanKind = "artisan",
        trace_id: str | None = None, binder: Callable[[], Awaitable[TraceBinding | None]] | None = None,
    ) -> None:
        """
        Args:
            store: span 存储。
            owner: 会话主人；trace 归属于他。
            config: 上限配置。
            ref: 根执行 span 指向的引用（会话标识）。
            origin: trace 的来源。
            root_kind: 根执行 span 的类型（业务会话是 artisan，管家会话是 butler）。
            trace_id: 预先分配给第一条根 trace 的标识；None 时一律新生成。
            binder: 查本引擎的跨引擎绑定；None 表示不查（一律单独成 trace）。
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
        self._preset: str | None = trace_id
        self._binder = binder
        self._task: asyncio.Task[None] | None = None
        # 每个执行自上次动手以来对员工说的话；调用工具时取走成为旁白（ADR 0023）
        self._said: dict[str, str] = {}

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
            for key in self._finished:
                done = self._traces.get(key)
                # 有待补的“不完整”标记时先留着，下一次写出后再移除
                if done is not None and not done.pending and not done.dropped:
                    del self._traces[key]
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
        self._said.clear()

    def _handle(self, event: taifeng.EventMsg) -> None:
        """找到所属 trace，追加草稿；根执行结束时登记 trace 结束。"""
        identity = event.execution
        if identity is None or self._ended.get(identity.execution_id) == identity.attempt_id:
            return
        if event.msg.kind == "assistant_text":
            self._hear(identity.execution_id, event.msg.data.get("delta"))
            return
        trace = self._trace_for(event)
        if trace is None:
            # 记不了的执行：攒的话也作废，免得挂到之后无关的工具上
            self._said.pop(identity.execution_id, None)
            return
        found = self._narrated(identity.execution_id, event.msg.kind,
                               drafts(event, root_kind=self._root_kind, ref=self._ref))
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
                                  if v != trace.key}
            self._finished.append(trace.key)
            self._ended[identity.execution_id] = identity.attempt_id
            if len(self._ended) > _ENDED_MEMORY:
                self._ended.pop(next(iter(self._ended)))

    def _hear(self, execution_id: str, delta: object) -> None:
        """记下执行对员工说的一段话；非文本增量忽略（不是正文就没有可记的）。"""
        if isinstance(delta, str) and delta:
            self._said[execution_id] = append(self._said.pop(execution_id, ""), delta)
            while len(self._said) > _SAID_MEMORY:
                self._said.pop(next(iter(self._said)))

    def _narrated(self, execution_id: str, kind: str, found: list[Draft]) -> list[Draft]:
        """工具开始时把攒下的话挂到第一个工具 span 上并清空；一轮结束时清空。"""
        if kind in _TURN_ENDS:
            self._said.pop(execution_id, None)
            return found
        if kind != "tool_call_started":
            return found
        said = narration(self._said.pop(execution_id, ""))
        if said is None:
            return found
        return [dataclasses.replace(d, say=said) if d.kind == "tool" else d for d in found]

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
        # 预分配的标识只给第一条从头开始的根 trace；孤儿执行不占用它
        fresh_root = parent is None and event.msg.kind == "turn_started"
        key = new_id("trc")
        preset = self._preset if fresh_root else None
        if fresh_root:
            self._preset = None
        trace = _Trace(key=key, trace_id=preset or key, title=identity.skill_id,
                       opened_at=event_time(event),
                       orphan=parent is not None or event.msg.kind != "turn_started",
                       bindable=fresh_root and preset is None and self._binder is not None)
        self._traces[key] = trace
        self._by_execution[identity.execution_id] = key
        return trace

    async def _write(self, trace: _Trace) -> None:
        """先确保 trace 已建，再按序号写一批。失败的批次不回填，只让 trace 标为不完整。"""
        if not trace.pending and not trace.dropped:
            return
        batch, trace.pending = trace.pending, []
        self._pending_total -= len(batch)
        try:
            if trace.next_seq is None:
                await self._bind(trace)
                record = await self._store.open_trace(
                    self._owner, trace_id=trace.trace_id, origin=self._origin,
                    title=trace.title, at=trace.opened_at,
                )
                trace.next_seq = record.sources.get(trace.key, 0) + 1
            facts = self._numbered(trace, batch)
            if trace.dropped or trace.orphan:
                await self._store.mark_incomplete(self._owner, trace.trace_id)
                trace.dropped = trace.orphan = False
            if facts:
                await self._store.apply(
                    self._owner, trace.trace_id, facts, source=trace.key,
                    max_spans=self._config.max_spans_per_trace,
                    max_attempts=self._config.max_attempts_per_span,
                )
        except Exception:
            trace.dropped = True
            logger.exception("执行追踪写入失败（trace %s，%d 条事实）", trace.trace_id, len(batch))

    async def _bind(self, trace: _Trace) -> None:
        """从头开始的根 trace 在第一次写出前查一次持久绑定；查询失败照常抛出（标不完整）。"""
        if not trace.bindable or self._binder is None:
            return
        trace.bindable = False
        binding = await self._binder()
        if binding is not None:
            trace.trace_id, trace.parent_span = binding.trace_id, binding.parent_span_id

    def _numbered(self, trace: _Trace, batch: list[Draft]) -> list[SpanFact]:
        """逐条编号；构造不出合法事实的草稿跳过并标不完整（序号照样前进）。

        绑定到别处的 trace 时，新建且没有父级的 span（根执行）挂到绑定的父 span 下。
        """
        assert trace.next_seq is not None
        facts = []
        for draft in batch:
            fields = draft.fields()
            if trace.parent_span is not None and fields.get("kind") is not None \
                    and fields.get("parent_span_id") is None:
                fields["parent_span_id"] = trace.parent_span
            try:
                facts.append(SpanFact(seq=trace.next_seq, **fields))
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
    store: ControlPlaneStore, wiring: TraceWiring | None,
    tenant_id: str, session_id: str, user_id: str | None,
) -> TraceCollector | None:
    """租户开启追踪且会话有主人时，给这个引擎配一个采集器；否则不采集。"""
    if wiring is None or user_id is None or not wiring.config.allows(tenant_id):
        return None
    owner = TraceOwner(tenant_id=tenant_id, user_id=user_id)
    binder = wiring.binder

    async def bind() -> TraceBinding | None:
        """查这个会话的跨引擎绑定。"""
        assert binder is not None
        return await binder.bind(owner, session_id)

    return TraceCollector(store.trace_store, owner, wiring.config, ref=session_id,
                          root_kind=wiring.root_kind, trace_id=wiring.trace_id,
                          binder=None if binder is None else bind)
