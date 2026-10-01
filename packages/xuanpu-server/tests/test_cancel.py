"""停止生成：取消会话里在途的一轮，经平台、经 HTTP 与 SDK。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from xuanpu_sdk import XuanpuClient, XuanpuError
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import InvalidRequestError, NotFoundError, SessionBusyError
from xuanpu_server.platform import ROOT_PRINCIPAL
from xuanpu_server.quota import usage_day

if TYPE_CHECKING:
    from collections.abc import Callable


async def wait_until(condition: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """轮询直到条件成立；超时就让用例失败。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"等了 {timeout} 秒条件仍不成立")
        await asyncio.sleep(0.01)


def reader(turn: Any, into: list[Any]) -> asyncio.Future[None]:
    """在另一个任务里读一轮对话的事件，逐条放进 ``into``。"""

    async def read() -> None:
        """读到这一轮结束。"""
        async for event in turn.stream.events():
            into.append(event)

    return asyncio.ensure_future(read())


async def audit_kinds(world: Any, session_id: str) -> list[str]:
    """会话的全部审计记录的类型，按写入先后。"""
    records = await world.platform.store.query_audit(
        "acme", AuditQuery(session_id=session_id, limit=1000)
    )
    return [record.kind for record in records]


async def test_cancel_stops_the_running_turn(world: Any, step: Any, read_turn: Any) -> None:
    """在途的一轮被停下：以 turn_completed（end_reason=cancelled）收尾，会话还能继续用。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    gate = asyncio.Event()
    world.models.script(step(text="写到一半", wait=gate), step(text="下一轮"))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "写长文")
    events: list[Any] = []
    task = reader(turn, events)
    await wait_until(lambda: any(e.msg.kind == "assistant_text" for e in events))

    cancelled = await world.platform.sessions.cancel(world.member, session.session_id)
    assert cancelled == [turn.submission_id]
    await asyncio.wait_for(task, 10)
    last = events[-1].msg
    assert last.kind == "turn_completed"
    assert last.data["end_reason"] == "cancelled"
    assert (last.data["success"], last.data["cancel_reason"]) == (False, "requested")
    assert world.platform.sessions.in_flight_turns("acme") == 0

    done = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "再来")
    )
    assert done[-1].msg.kind == "turn_completed"
    assert done[-1].msg.data.get("end_reason") != "cancelled"
    records = await world.platform.store.query_audit(
        "acme", AuditQuery(session_id=session.session_id, limit=1000)
    )
    requested = [record for record in records if record.kind == "turn_cancel_requested"]
    assert [record.data["submission_ids"] for record in requested] == [[turn.submission_id]]
    assert requested[0].actor == world.member.actor
    # CancelTurn 自己那个 submission 只有一条引擎日志：不登记事件流，也不算在途
    logs = [
        record
        for record in records
        if record.kind == "engine_log" and turn.submission_id in str(record.data.get("message"))
    ]
    assert len(logs) == 1
    assert logs[0].data["submission_id"] != turn.submission_id
    texts = [record.data["text"] for record in records if record.kind == "turn_text"]
    assert texts == ["写到一半", "下一轮"]


async def test_cancel_with_nothing_running(world: Any, step: Any, read_turn: Any) -> None:
    """没有在途对话：返回空列表，不报错，不记审计，也不为了取消去打开引擎。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    # 还没对话过：本实例没打开它，也没人持有
    assert await world.platform.sessions.cancel(world.member, session.session_id) == []
    assert (await world.platform.sessions.get(world.member, session.session_id)).owner is None

    # 对话过、本实例还持有着，但这一轮已经结束
    world.models.script(step(text="好了"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    assert await world.platform.sessions.cancel(world.member, session.session_id) == []
    assert "turn_cancel_requested" not in await audit_kinds(world, session.session_id)


async def test_cancel_unknown_submission(world: Any, step: Any, read_turn: Any) -> None:
    """指定的 submission 不在途（已经结束的、根本没有的）：请求不合法。

    与引擎是否还留在本实例无关：会话没人持有时，指定的 submission 同样不在途。
    """
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    with pytest.raises(InvalidRequestError):
        await world.platform.sessions.cancel(
            world.member, session.session_id, submission_id="sub_nope"
        )
    world.models.script(step(text="好了"))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    await read_turn(turn)
    for submission_id in (turn.submission_id, "sub_nope"):
        with pytest.raises(InvalidRequestError):
            await world.platform.sessions.cancel(
                world.member, session.session_id, submission_id=submission_id
            )
    assert "turn_cancel_requested" not in await audit_kinds(world, session.session_id)


async def test_cancel_specific_submission(world: Any, step: Any) -> None:
    """给出在途的那个 submission 时只停它：排队的第二轮被停，正在跑的第一轮照常跑完。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    gate = asyncio.Event()
    world.models.script(step(text="第一轮", wait=gate), step(text="不该出现"))
    first = await world.platform.sessions.send_message(world.member, session.session_id, "一")
    first_events: list[Any] = []
    first_task = reader(first, first_events)
    await wait_until(lambda: any(e.msg.kind == "assistant_text" for e in first_events))
    second = await world.platform.sessions.send_message(world.member, session.session_id, "二")
    second_events: list[Any] = []
    second_task = reader(second, second_events)
    await wait_until(lambda: any(e.msg.kind == "submission_queued" for e in second_events))

    cancelled = await world.platform.sessions.cancel(
        world.member, session.session_id, submission_id=second.submission_id
    )
    assert cancelled == [second.submission_id]
    await asyncio.wait_for(second_task, 10)
    # 排队中被停的一轮没开始跑：内核以 turn_failed（kind=cancelled）收尾
    assert second_events[-1].msg.kind == "turn_failed"
    assert second_events[-1].msg.data["kind"] == "cancelled"
    assert not first_task.done()

    gate.set()
    await asyncio.wait_for(first_task, 10)
    assert first_events[-1].msg.kind == "turn_completed"
    assert first_events[-1].msg.data["end_reason"] != "cancelled"
    assert world.platform.sessions.in_flight_turns("acme") == 0
    assert world.models.steps  # 第二轮没有采样，它的脚本还没被取用


async def test_cancel_when_another_instance_holds_the_session(world: Any) -> None:
    """会话由别的实例持有：报 session_busy 并带上持有者；无人持有（含租约已过期）时返回空。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    store = world.platform.store
    await store.acquire_lease(
        "acme", session.session_id, owner="inst-other", now=time.time(), ttl_seconds=60
    )
    with pytest.raises(SessionBusyError) as caught:
        await world.platform.sessions.cancel(world.member, session.session_id)
    assert caught.value.owner == "inst-other"

    assert await store.release_lease("acme", session.session_id, owner="inst-other")
    assert await world.platform.sessions.cancel(world.member, session.session_id) == []

    # 别的实例的租约早已过期：它不再算持有者，同样没有在途对话
    await store.acquire_lease(
        "acme", session.session_id, owner="inst-other", now=time.time() - 120, ttl_seconds=60
    )
    assert await world.platform.sessions.cancel(world.member, session.session_id) == []
    record = await world.platform.sessions.get(world.member, session.session_id)
    assert record.owner == "inst-other"  # 取消不去抢租约
    assert "turn_cancel_requested" not in await audit_kinds(world, session.session_id)


async def test_cancel_respects_tenant_isolation(world: Any, step: Any) -> None:
    """别的租户的会话按不存在处理，在途的对话不受影响。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    gate = asyncio.Event()
    world.models.script(step(text="进行中", wait=gate))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "跑")
    events: list[Any] = []
    task = reader(turn, events)
    await wait_until(lambda: any(e.msg.kind == "assistant_text" for e in events))

    await world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id="globex", name="Globex")
    issued = await world.platform.issue_key(
        ROOT_PRINCIPAL, tenant_id="globex", name="app", role="tenant_member"
    )
    stranger = await world.platform.authenticate(issued.key)
    with pytest.raises(NotFoundError):
        await world.platform.sessions.cancel(stranger, session.session_id)
    with pytest.raises(NotFoundError):
        await world.platform.sessions.cancel(
            stranger, session.session_id, submission_id=turn.submission_id
        )

    gate.set()
    await asyncio.wait_for(task, 10)
    assert events[-1].msg.data["end_reason"] != "cancelled"


async def test_cancelled_turn_is_still_metered(world: Any, step: Any) -> None:
    """被停的一轮照样记一轮用量；卡在半路的那次采样没有收尾，内核报不出它的 token。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    gate = asyncio.Event()
    world.models.script(step(text="写到一半", wait=gate))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "写")
    events: list[Any] = []
    task = reader(turn, events)
    await wait_until(lambda: any(e.msg.kind == "assistant_text" for e in events))
    await world.platform.sessions.cancel(world.member, session.session_id)
    await asyncio.wait_for(task, 10)

    assert events[-1].msg.data["usage"]["total_tokens"] == 0
    today = usage_day(time.time())
    usage = await world.platform.store.list_usage("acme", since=today, until=today)
    assert [(record.turns, record.total_tokens) for record in usage] == [(1, 0)]


async def test_cancel_over_http(world: Any, step: Any) -> None:
    """经 HTTP 与 SDK：在途的一轮被停下，事件流以 cancelled 收尾；不在途的 submission 是 400。"""
    await world.publish()
    transport = httpx.ASGITransport(app=create_app(world.platform))
    gate = asyncio.Event()
    world.models.script(step(text="写到一半", wait=gate))
    async with XuanpuClient("http://xuanpu", world.member_key, transport=transport) as client:
        session = await client.sessions.create("support-desk")

        async def read() -> list[Any]:
            """读完这一轮的事件流（ASGI 传输要等响应结束才交出整个响应体）。"""
            return [event async for event in client.sessions.send(session.session_id, "写长文")]

        task = asyncio.ensure_future(read())
        await wait_until(
            lambda: world.platform.sessions.in_flight_turns("acme") == 1
            and bool(world.models.requests)
        )
        cancelled = await client.sessions.cancel(session.session_id)
        events = await asyncio.wait_for(task, 10)
        assert cancelled == [events[0].submission_id]
        assert events[-1].kind == "turn_completed"
        assert events[-1].data["end_reason"] == "cancelled"

        with pytest.raises(XuanpuError) as caught:
            await client.sessions.cancel(session.session_id, submission_id="sub_nope")
        assert (caught.value.status, caught.value.code) == (400, "invalid_request")

    # 请求体可以省略；没有在途对话时返回空列表
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as raw:
        response = await raw.post(
            f"/v1/sessions/{session.session_id}/cancel",
            headers={"Authorization": f"Bearer {world.member_key}"},
        )
    assert (response.status_code, response.json()) == (200, {"cancelled": []})
