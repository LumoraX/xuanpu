"""内核事件兼容契约（R5-A1）：没有执行身份的旧事件、带新增数据键的事件都不让采集出错。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import taifeng

from server.engine.tracing.collector import TraceCollector
from server.engine.tracing.config import ExecutionTraceConfig
from server.engine.tracing.events import drafts
from server.engine.tracing.memory import MemorySpanStore

from .helpers import ALICE

CONFIG = ExecutionTraceConfig(enabled=True, tenants=("acme",))


def _event(kind: str, data: dict[str, object], execution: dict[str, object] | None = None,
           ) -> taifeng.EventMsg:
    """按内核稳定层的事件模型校验构造一条事件。"""
    return taifeng.EventMsg.model_validate({
        "submission_id": "sub_1", "msg": {"kind": kind, "data": data},
        "timestamp": datetime.now(UTC), "seq": 1, "execution": execution})


def test_event_without_identity_yields_no_facts() -> None:
    """升级前的事件（``execution`` 为空）可以回放：不产生事实、不报错。"""
    for kind in ("turn_started", "turn_completed", "tool_call_started", "execution_updated"):
        assert drafts(_event(kind, {}), root_kind="artisan", ref="ses_1") == []


async def test_collector_ignores_legacy_and_tolerates_new_keys() -> None:
    """旧事件不建 trace；新事件里多出的数据键被忽略，照常折成 span。"""
    store = MemorySpanStore()
    collector = TraceCollector(store, ALICE, CONFIG, ref="ses_1")
    collector.handle(_event("turn_started", {"future_field": 1}))
    await collector.flush()
    assert (await store.list(ALICE)).total == 0
    identity = {"execution_id": "ex1", "attempt_id": "at1", "thread_id": "th1", "skill_id": "triage",
                "model": "m"}
    collector.handle(_event("turn_started", {"future_field": 1}, identity))
    collector.handle(_event("turn_completed", {"end_reason": "done", "another_new_key": "x"}, identity))
    await collector.flush()
    await asyncio.sleep(0)
    [trace] = (await store.list(ALICE)).items
    view = await store.get(ALICE, trace.trace_id)
    assert [s.kind for s in view.spans] == ["artisan"]


async def test_engine_closed_mid_execution_marks_gap() -> None:
    """引擎在执行中途被关闭（进程终止前的收尾）：未结束的 trace 仍能读出，并标记不完整。"""
    store = MemorySpanStore()
    collector = TraceCollector(store, ALICE, CONFIG, ref="ses_1")
    identity = {"execution_id": "ex9", "attempt_id": "at9", "thread_id": "th9", "skill_id": "triage",
                "model": "m"}
    collector.handle(_event("turn_started", {}, identity))
    await collector.flush()
    await collector.close()
    [trace] = (await store.list(ALICE)).items
    view = await store.get(ALICE, trace.trace_id)
    assert view.trace.incomplete and view.active
    assert [s.state for s in view.spans] == ["running"]
