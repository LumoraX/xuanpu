"""派工与追踪的关联：派工 span 随版本写入、写入失败不影响派工；绑定按会话或任务查出。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from xuanpu_server.butler.memory import MemoryDispatchStore
from xuanpu_server.butler.models import ArtisanRef, DispatchRecord, dispatch_fingerprint
from xuanpu_server.butler.trace_link import (
    DispatchTraceBinder,
    TracedDispatchStore,
    dispatch_span,
)
from xuanpu_server.tracing.config import ExecutionTraceConfig
from xuanpu_server.tracing.memory import MemorySpanStore
from xuanpu_server.tracing.models import TraceOwner

T0 = datetime(2026, 11, 14, 8, tzinfo=UTC)
ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
REF = ArtisanRef(package="support-desk", version="1.0.0", digest="ab" * 32, entry="triage",
                 decision_ref="catalog:support-desk@1.0.0")
ON = ExecutionTraceConfig(enabled=True, tenants=("acme",))
LIMITS = {"max_per_trace": 4, "max_active_traces": 4}


def _record(dispatch_id: str = "dsp_1") -> DispatchRecord:
    """管家根 span 下的一次派工。"""
    return DispatchRecord(
        dispatch_id=dispatch_id, tenant_id="acme", user_id="usr_alice", request_id=dispatch_id,
        trace_id="trc_1", parent_span_id="ex.root", artisan=REF, host="session",
        instruction="查工单", created_at=T0, updated_at=T0,
        input_fingerprint=dispatch_fingerprint(trace_id="trc_1", artisan=REF,
                                               instruction="查工单", materials=()))


async def test_dispatch_span_follows_versions() -> None:
    """建、运行、结束各写一条事实：span 挂在管家根下、状态推进、记录完整。"""
    traces = MemorySpanStore()
    store = TracedDispatchStore(MemoryDispatchStore(), traces, lambda: ON)
    await store.create(ALICE, _record(), **LIMITS)
    await store.update(ALICE, "dsp_1", expected_version=1, to="running", at=T0, host_ref="ses_1")
    await store.update(ALICE, "dsp_1", expected_version=2, to="completed", at=T0)
    await store.update(ALICE, "dsp_1", expected_version=3, to="delivered", at=T0)
    view = await traces.get(ALICE, "trc_1")
    [span] = view.spans
    assert (span.span_id, span.kind, span.parent_span_id) == ("dp.dsp_1", "dispatch", "ex.root")
    assert span.state == "completed" and span.ref == "dsp_1"
    assert view.trace.sources == {"dp.dsp_1": 4} and not view.trace.incomplete


async def test_disabled_tenant_writes_nothing() -> None:
    """追踪未开启的租户不建 trace。"""
    traces = MemorySpanStore()
    store = TracedDispatchStore(MemoryDispatchStore(), traces, ExecutionTraceConfig)
    await store.create(ALICE, _record(), **LIMITS)
    assert (await traces.list(ALICE)).total == 0


async def test_trace_failure_does_not_block_dispatch_and_leaves_gap() -> None:
    """一次追踪写入失败：派工照常推进；下一次写入跳号，trace 标不完整。"""
    traces = MemorySpanStore()
    store = TracedDispatchStore(MemoryDispatchStore(), traces, lambda: ON)
    await store.create(ALICE, _record(), **LIMITS)
    original = traces.apply

    async def broken(*args: Any, **kwargs: Any) -> Any:
        """模拟一次写入失败。"""
        raise RuntimeError("库暂时不可用")

    traces.apply = broken  # type: ignore[method-assign]
    running = await store.update(ALICE, "dsp_1", expected_version=1, to="running", at=T0,
                                 host_ref="ses_1")
    assert running.state == "running"
    traces.apply = original  # type: ignore[method-assign]
    await store.update(ALICE, "dsp_1", expected_version=2, to="completed", at=T0)
    assert (await traces.get(ALICE, "trc_1")).trace.incomplete


def _binder(dispatches: MemoryDispatchStore, sessions: dict[str, Any],
            tasks: dict[str, str]) -> DispatchTraceBinder:
    """用替身控制面与任务存储构造绑定查询。"""
    async def get_session(tenant_id: str, session_id: str) -> Any:
        """按标识取会话。"""
        return sessions.get(session_id)

    async def get_task(who: Any, task_id: str) -> Any:
        """按标识取任务；只给请求标识。"""
        return SimpleNamespace(request_id=tasks[task_id])

    control = SimpleNamespace(dispatch_store=dispatches, get_session=get_session)
    return DispatchTraceBinder(control, SimpleNamespace(get=get_task))  # type: ignore[arg-type]


async def test_binder_resolves_session_and_task_hosts() -> None:
    """会话承载按 host_ref；任务承载按任务请求标识；其他会话不绑定。"""
    dispatches = MemoryDispatchStore()
    await dispatches.create(ALICE, _record("dsp_s"), **LIMITS)
    await dispatches.update(ALICE, "dsp_s", expected_version=1, to="running", at=T0,
                            host_ref="ses_artisan")
    await dispatches.create(ALICE, _record("dsp_t"), **LIMITS)
    sessions = {"ses_task": SimpleNamespace(work_task_id="task_1"),
                "ses_other_task": SimpleNamespace(work_task_id="task_2"),
                "ses_plain": SimpleNamespace(work_task_id=None)}
    binder = _binder(dispatches, sessions, {"task_1": "butler:dsp_t", "task_2": "manual-1"})
    bound = await binder.bind(ALICE, "ses_artisan")
    assert bound is not None and bound.parent_span_id == dispatch_span("dsp_s")
    bound = await binder.bind(ALICE, "ses_task")
    assert bound is not None and bound.parent_span_id == dispatch_span("dsp_t")
    assert bound.trace_id == "trc_1"
    for unbound in ("ses_plain", "ses_other_task", "ses_missing"):
        assert await binder.bind(ALICE, unbound) is None
    bob = TraceOwner(tenant_id="acme", user_id="usr_bob")
    assert await binder.bind(bob, "ses_artisan") is None
