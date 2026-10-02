"""审批待办与事件回放：以审计为准，各实例共享。"""

from __future__ import annotations

from typing import Any

import httpx
from xuanpu_sdk import XuanpuClient
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import AuditQuery, AuditRecord, InMemoryControlPlane
from xuanpu_server.runtime.session_history import pending_suspension


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


async def test_rejected_resume_ends_its_stream(world: Any, step: Any, read_turn: Any) -> None:
    """没有挂起时送回审批：内核以 suspension_resolve_rejected 拒绝，事件流就此收尾、不算在途。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="好了"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    resumed = await world.platform.sessions.resume(
        world.member, session.session_id, {"req_nope": {"granted": True}}
    )
    events = await read_turn(resumed, timeout=10)
    assert [event.msg.kind for event in events] == ["suspension_resolve_rejected"]
    assert events[0].msg.data["reason"] == "no_active_suspension"
    assert events[0].submission_id == resumed.submission_id
    assert world.platform.sessions.in_flight_turns("acme") == 0
    # 没有采样：不落回复全文，也不计一轮
    replayed = await world.platform.sessions.replay(
        world.member, session.session_id, resumed.submission_id
    )
    assert [event["kind"] for event in replayed] == ["suspension_resolve_rejected"]
    usage = await world.platform.store.list_usage("acme", since="2000-01-01", until="2999-12-31")
    assert sum(record.turns for record in usage) == 1


async def test_partial_resume_ends_its_stream(world: Any, step: Any, read_turn: Any) -> None:
    """一次挂起两个待办、只送回一个：这次送回以 suspension_partially_resolved 收尾，另一个还等着。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(
            calls=[
                ("c1", "mcp__crm__update_ticket", {"id": "T-1"}),
                ("c2", "mcp__crm__update_ticket", {"id": "T-2"}),
            ]
        ),
        step(text="都更新了"),
    )
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "更新两个")
    )
    first, second = await world.platform.sessions.pending(world.member, session.session_id)
    partial = await read_turn(
        await world.platform.sessions.resume(
            world.member, session.session_id, {first["request_id"]: {"granted": True}}
        ),
        timeout=10,
    )
    assert partial[-1].msg.kind == "suspension_partially_resolved"
    assert world.platform.sessions.in_flight_turns("acme") == 0
    remaining = await world.platform.sessions.pending(world.member, session.session_id)
    assert [item["request_id"] for item in remaining] == [second["request_id"]]

    done = await read_turn(
        await world.platform.sessions.resume(
            world.member, session.session_id, {second["request_id"]: {"granted": True}}
        ),
        timeout=10,
    )
    assert done[-1].msg.kind == "turn_completed"
    assert await world.platform.sessions.pending(world.member, session.session_id) == []


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


def engine_record(seq: int, kind: str, **data: Any) -> AuditRecord:
    """一条引擎事件的审计记录。"""
    return AuditRecord(
        tenant_id="acme",
        source="engine",
        kind=kind,
        at=float(seq),
        actor="engine",
        session_id="ses_1",
        stream="st_1",
        seq=seq,
        data=data,
    )


async def test_pending_reads_from_the_last_suspension_only() -> None:
    """待办只从最近一次挂起往后读：先按种类找到最后一次挂起，不再把更早的引擎事件整段翻一遍。"""
    store = InMemoryControlPlane()
    await store.append_audit(
        [
            engine_record(0, "turn_suspended", submission_id="sub_1", record_id="sr_1",
                          pending=[{"request_id": "r_old"}]),
            *[engine_record(seq, "tool_call_completed", submission_id="sub_1") for seq in range(1, 6)],
            engine_record(6, "turn_suspended", submission_id="sub_2", record_id="sr_2",
                          pending=[{"request_id": "r1"}, {"request_id": "r2"}]),
            engine_record(7, "suspension_partially_resolved", submission_id="sub_3",
                          record_id="sr_2", resolved_request_ids=["r1"]),
        ]
    )  # fmt: skip
    queries: list[AuditQuery] = []
    original = store.query_audit

    async def spy(tenant_id: str, query: AuditQuery) -> list[AuditRecord]:
        """记下每次查询的条件。"""
        queries.append(query)
        return await original(tenant_id, query)

    store.query_audit = spy  # type: ignore[method-assign]
    snapshot = await pending_suspension(store, "acme", "ses_1")
    assert [item["request_id"] for item in snapshot.requests] == ["r2"]
    assert (snapshot.submission_id, snapshot.settled) == ("sub_2", frozenset({"r1"}))
    # 第二次挂起是第 7 条记录：不按种类过滤的那次查询从它开始
    assert [q.after_id for q in queries if q.kind is None] == [6]
