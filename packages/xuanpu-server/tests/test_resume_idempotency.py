"""重复审批在实际工具链上不能制造第二次副作用。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from xuanpu_server.errors import ConflictError


@pytest.mark.parametrize("world", ["local", "docker"], indirect=True)
async def test_identical_resume_returns_original_submission(world: Any, step: Any, read_turn: Any) -> None:
    """第一次批准真实执行一次，再次批准只回放原提交。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="改好了"))
    await read_turn(await world.platform.sessions.send_message(world.member, session.session_id, "修改"))
    [pending] = await world.platform.sessions.pending(world.member, session.session_id)
    decision = {pending["request_id"]: {"granted": True}}
    first = await world.platform.sessions.resume(world.member, session.session_id, decision)
    await read_turn(first)
    replay = await world.platform.sessions.resume(world.member, session.session_id, decision)
    assert replay.submission_id == first.submission_id
    assert replay.replayed
    assert len(world.calls()) == 1


@pytest.mark.parametrize("world", ["local", "docker"], indirect=True)
async def test_concurrent_resume_has_single_tool_effect(world: Any, step: Any, read_turn: Any) -> None:
    """并发批准不会经审计读视图竞态造成两次工具调用。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="好了"))
    await read_turn(await world.platform.sessions.send_message(world.member, session.session_id, "改"))
    [pending] = await world.platform.sessions.pending(world.member, session.session_id)
    decision = {pending["request_id"]: {"granted": True}}
    results = await asyncio.gather(*[
        world.platform.sessions.resume(world.member, session.session_id, decision) for _ in range(2)
    ], return_exceptions=True)
    fresh = [item for item in results if not isinstance(item, Exception) and not getattr(item, "replayed", False)]
    assert len(fresh) == 1
    assert all(not isinstance(item, Exception) or isinstance(item, ConflictError) for item in results)
    await read_turn(fresh[0])
    assert len(world.calls()) == 1


async def test_applied_tool_with_failed_receipt_is_not_reexecuted(world: Any, step: Any, read_turn: Any, monkeypatch: Any) -> None:
    """外部工具已写入、结算回执故障后，只允许核实结果，不能再次执行。"""
    import pytest
    await world.publish()
    sessions = world.platform.sessions
    session = await sessions.create(world.member, package="support-desk")
    world.models.script(step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="完成"))
    await read_turn(await sessions.send_message(world.member, session.session_id, "修改"))
    [pending] = await sessions.pending(world.member, session.session_id)
    answer = {pending["request_id"]: {"granted": True}}
    receipts = world.platform.store.operation_receipts
    original, saved_unknown = receipts.settle, asyncio.Event()
    async def settle(tenant: str, operation: str, state: str, audit: Any, *, at: float) -> None:
        if state == "applied":
            raise RuntimeError("receipt write failed after external action")
        await original(tenant, operation, state, audit, at=at)
        if state == "outcome_unknown":
            saved_unknown.set()
    monkeypatch.setattr(receipts, "settle", settle)
    await read_turn(await sessions.resume(world.member, session.session_id, answer))
    await asyncio.wait_for(saved_unknown.wait(), 5)
    with pytest.raises(ConflictError, match="核实"):
        await sessions.resume(world.member, session.session_id, answer)
    assert len(world.calls()) == 1


async def test_http_resume_replay_uses_original_sse_submission(world: Any, step: Any, read_turn: Any) -> None:
    """HTTP 重复批准返回原标识和审计事件，不创建第二条内核提交。"""
    import httpx
    from xuanpu_server.api import create_app
    await world.publish()
    sessions = world.platform.sessions
    session = await sessions.create(world.member, package="support-desk")
    world.models.script(step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="完成"))
    await read_turn(await sessions.send_message(world.member, session.session_id, "修改"))
    [pending] = await sessions.pending(world.member, session.session_id)
    answer = {"resolutions": {pending["request_id"]: {"granted": True}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(world.platform)), base_url="http://test",
        headers={"Authorization": "Bearer " + world.member_key}) as client:
        first = await client.post(f"/v1/sessions/{session.session_id}/resume", json=answer)
        replay = await client.post(f"/v1/sessions/{session.session_id}/resume", json=answer)
    assert first.status_code == replay.status_code == 200
    assert first.headers["x-submission-id"] == replay.headers["x-submission-id"]
    assert replay.headers["x-xuanpu-replayed"] == "true"
    assert "event: end" in replay.text
    assert len(world.calls()) == 1
