"""审批待办与事件回放：以审计为准，各实例共享。"""

from __future__ import annotations

from typing import Any

import httpx
from xuanpu_sdk import XuanpuClient
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import AuditQuery


async def test_pending_follows_suspend_and_resume(world: Any, step: Any, read_turn: Any) -> None:
    """挂起后能查到待办；批准后待办清空；下一次挂起又出现。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    assert await world.platform.sessions.pending(world.member, session.session_id) == []

    world.models.script(
        step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]),
        step(text="好了"),
        step(calls=[("c2", "mcp__crm__update_ticket", {"id": "T-2"})]),
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "更新")
    )
    pending = await world.platform.sessions.pending(world.member, session.session_id)
    assert [item["detail"]["args"] for item in pending] == [{"id": "T-1"}]
    request_id = pending[0]["request_id"]
    assert request_id == events[-1].msg.data["pending"][0]["request_id"]

    await read_turn(
        await world.platform.sessions.resume(
            world.member, session.session_id, {request_id: {"granted": True}}
        )
    )
    assert await world.platform.sessions.pending(world.member, session.session_id) == []

    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "再更新")
    )
    pending = await world.platform.sessions.pending(world.member, session.session_id)
    assert [item["detail"]["args"] for item in pending] == [{"id": "T-2"}]


async def test_replay_returns_audited_events_with_full_text(
    world: Any, step: Any, read_turn: Any
) -> None:
    """回放拿到本轮的事件与拼好的回复全文；``after_seq`` 只取之后的。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="第一句。第二句。"))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    live = await read_turn(turn)

    replayed = await world.platform.sessions.replay(
        world.member, session.session_id, turn.submission_id
    )
    kinds = [event["kind"] for event in replayed]
    assert kinds[0] == "turn_started"
    assert kinds[-2:] == ["turn_completed", "turn_text"]
    assert "assistant_text" not in kinds
    assert replayed[-1]["data"] == {"text": "第一句。第二句。"}
    assert all("submission_id" not in event["data"] for event in replayed)

    last_seq = [event.seq for event in live if event.msg.kind == "turn_completed"][0]
    tail = await world.platform.sessions.replay(
        world.member, session.session_id, turn.submission_id, after_seq=last_seq - 1
    )
    assert [event["kind"] for event in tail] == ["turn_completed", "turn_text"]
    assert await world.platform.sessions.replay(world.member, session.session_id, "sub_nope") == []


async def test_audit_query_by_submission(world: Any, step: Any, read_turn: Any) -> None:
    """审计可以按一轮对话过滤，两个实现口径一致（Postgres 由契约测试另行覆盖）。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="a"), step(text="b"))
    first = await world.platform.sessions.send_message(world.member, session.session_id, "1")
    await read_turn(first)
    second = await world.platform.sessions.send_message(world.member, session.session_id, "2")
    await read_turn(second)
    records = await world.platform.store.query_audit(
        "acme", AuditQuery(submission_id=first.submission_id, limit=1000)
    )
    assert records
    assert all(record.data["submission_id"] == first.submission_id for record in records)


async def test_pending_and_replay_over_http(world: Any, step: Any) -> None:
    """经 HTTP 与 SDK。"""
    await world.publish()
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with XuanpuClient("http://xuanpu", world.member_key, transport=transport) as client:
        session = await client.sessions.create("support-desk")
        world.models.script(
            step(text="要更新", calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})])
        )
        events = [event async for event in client.sessions.send(session.session_id, "更新")]
        pending = await client.sessions.pending(session.session_id)
        assert pending[0]["request_id"] == events[-1].data["pending"][0]["request_id"]
        replayed = await client.sessions.replay(session.session_id, events[0].submission_id)
        assert replayed[-1] == {
            "kind": "turn_text", "seq": None, "at": replayed[-1]["at"], "data": {"text": "要更新"},
        }  # fmt: skip
