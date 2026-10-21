"""取消覆盖引擎准备窗口，并在提交前重新检查归属状态。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest
from xuanpu_server.errors import ConflictError


async def test_cancel_during_engine_preparation_does_not_start_model(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """准备阶段还没有 submission，但停止意图必须阻止随后开始采样。"""
    await world.publish()
    sessions = world.platform.sessions
    session = await sessions.create(world.member, package="support-desk")
    entered, release = asyncio.Event(), asyncio.Event()
    original = sessions._start_engine
    async def prepare(record: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(record)
    monkeypatch.setattr(sessions, "_start_engine", prepare)
    pending = asyncio.create_task(sessions.send_message(world.member, session.session_id, "不应开始"))
    await asyncio.wait_for(entered.wait(), 5)
    stopped = await sessions.cancel(world.member, session.session_id)
    release.set()
    result = await asyncio.gather(pending, return_exceptions=True)
    assert isinstance(result[0], ConflictError)
    assert getattr(stopped, "preparing_cancelled", False)
    assert world.models.requests == []


async def test_shutdown_waits_for_cold_preparation(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭先取消并等准备任务退出，再允许资源与实例锁被释放。"""
    await world.publish()
    sessions = world.platform.sessions
    session = await sessions.create(world.member, package="support-desk")
    entered, release = asyncio.Event(), asyncio.Event()
    original = sessions._start_engine
    async def prepare(record: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(record)
    monkeypatch.setattr(sessions, "_start_engine", prepare)
    pending = asyncio.create_task(sessions.send_message(world.member, session.session_id, "不要复活"))
    await asyncio.wait_for(entered.wait(), 5)
    await world.platform.close()
    release.set()
    await asyncio.gather(pending, return_exceptions=True)
    assert sessions.held_sessions() == 0
    assert world.platform.runtimes.live_runtimes() == 0
    saved = await world.platform.store.get_session(session.tenant_id, session.session_id)
    assert saved.owner is None


async def test_running_to_suspended_cancel_preserves_approval(world: Any, step: Any, read_turn: Any, monkeypatch: Any) -> None:
    """确认在途后恰好挂起，停止生成仍不能隐式核销审批。"""
    import taifeng
    await world.publish()
    sessions = world.platform.sessions
    session = await sessions.create(world.member, package="support-desk")
    gate, text_seen, suspended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    world.models.script(step(text="准备审批", wait=gate,
        calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="已修改"))
    turn = await sessions.send_message(world.member, session.session_id, "修改")
    async def consume() -> None:
        async for event in turn.stream.events():
            if event.msg.kind == "assistant_text":
                text_seen.set()
            if event.msg.kind == "turn_suspended":
                suspended.set()
    reader = asyncio.create_task(consume())
    await asyncio.wait_for(text_seen.wait(), 5)
    engine = sessions._live[(session.tenant_id, session.session_id)].engine
    original = engine.submit
    async def submit(op: Any) -> str:
        if isinstance(op, taifeng.CancelTurn):
            gate.set()
            await asyncio.wait_for(suspended.wait(), 5)
            while turn.submission_id in engine.introspect()["pending_submissions"]:
                await asyncio.sleep(0)
        return await original(op)
    monkeypatch.setattr(engine, "submit", submit)
    await asyncio.wait_for(sessions.cancel(world.member, session.session_id), 10)
    await reader
    [pending] = await sessions.pending(world.member, session.session_id)
    result = await read_turn(await sessions.resume(world.member, session.session_id,
        {pending["request_id"]: {"granted": True}}))
    assert result[-1].msg.kind == "turn_completed"
    assert len(world.calls()) == 1
