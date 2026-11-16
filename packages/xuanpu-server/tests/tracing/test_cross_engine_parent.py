"""跨引擎父子：管家派给会话承载的匠人，匠人的根执行挂在派工 span 下、同一条 trace；
未经派工的旧会话照旧单独成 trace，不猜父级。"""

from __future__ import annotations

import asyncio
from typing import Any

from xuanpu_server.butler.service import ButlerMessageBody
from xuanpu_server.butler.trace_link import dispatch_span
from xuanpu_server.tracing.models import TraceOwner

from .test_platform_tracing import _employee


def enable(world: Any) -> None:
    """测试内装配：追踪与管家都对 acme 开启。"""
    from dataclasses import replace

    from xuanpu_server.butler.config import ButlerConfig
    from xuanpu_server.tracing.config import ExecutionTraceConfig

    platform = world.platform
    platform.runtimes._defaults = replace(  # noqa: SLF001 —— 测试内装配
        platform.runtimes.defaults,
        execution_trace=ExecutionTraceConfig(enabled=True, tenants=("acme",)))
    platform.butler_config = ButlerConfig(enabled=True, tenants=("acme",))


async def employee(world: Any) -> Any:
    """员工 Alice。"""
    return await _employee(world, "usr_alice")


ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
INSTRUCTION = "INSTR-查询工单 T-1 的进度"


async def _settled(world: Any, trace_id: str, *, spans: int) -> Any:
    """等采集器批量写出（每 0.5 秒一次），直到 span 数量与终态都到位。"""
    store = world.platform.store.trace_store
    for _ in range(100):
        view = await store.get(ALICE, trace_id)
        if len(view.spans) >= spans and not view.active:
            return view
        await asyncio.sleep(0.1)
    raise AssertionError(f"trace 未在时限内写完：{view}")


async def test_artisan_root_hangs_under_dispatch_span(world: Any, step: Any) -> None:
    """管家根 → 派工 span → 匠人根，同一条 trace、记录完整；派工 span 随状态推进。"""
    enable(world)
    await world.publish()
    alice = await employee(world)
    world.models.script_for("INSTR-", step(text="T-1 已处理完"))
    world.models.script(
        step(calls=[("c1", "dispatch_artisan", {"package": "support-desk", "entry": "triage",
                                                 "instruction": INSTRUCTION})]),
        step(text="已交给工单匠人。"))
    receipt = await world.platform.butler.message(
        alice, ButlerMessageBody(request_id="x1", text="帮我看看工单 T-1"))
    [record] = await world.platform.dispatches.list_for_trace(ALICE, receipt["trace_id"])
    view = await _settled(world, receipt["trace_id"], spans=5)
    # 管家的第一轮派工；匠人结束后管家被唤醒，在同一条 trace 里另跑一轮（也是根）
    butler_roots = [s for s in view.spans if s.kind == "butler"]
    assert all(s.parent_span_id is None for s in butler_roots) and len(butler_roots) == 2
    butler_root = min(butler_roots, key=lambda s: s.started_at)
    [dispatch] = [s for s in view.spans if s.kind == "dispatch"]
    [artisan] = [s for s in view.spans if s.kind == "artisan"]
    assert dispatch.span_id == dispatch_span(record.dispatch_id)
    assert dispatch.parent_span_id == butler_root.span_id and dispatch.ref == record.dispatch_id
    assert artisan.parent_span_id == dispatch.span_id and artisan.ref == record.host_ref
    assert artisan.state == "completed"
    assert not view.trace.incomplete, view.trace
    assert len(view.trace.sources) >= 3
    traces = await world.platform.store.trace_store.list(ALICE)
    assert traces.total == 1


async def test_unbound_session_stays_its_own_trace(world: Any, step: Any, read_turn: Any) -> None:
    """员工直接开的业务会话（没有派工记录）：单独一条 trace，根 span 无父级。"""
    enable(world)
    await world.publish()
    alice = await employee(world)
    session = await world.platform.sessions.create(alice, package="support-desk")
    world.models.script(step(text="收到"))
    await read_turn(await world.platform.sessions.send_message(alice, session.session_id, "你好"))
    await world.platform.sessions.close(alice, session.session_id)
    page = await world.platform.store.trace_store.list(ALICE)
    assert page.total == 1
    view = await world.platform.store.trace_store.get(ALICE, page.items[0].trace_id)
    [root] = [s for s in view.spans if s.kind == "artisan"]
    assert root.parent_span_id is None and not view.trace.incomplete
