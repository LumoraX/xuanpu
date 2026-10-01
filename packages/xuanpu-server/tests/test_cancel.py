"""停止生成：取消会话里在途的一轮，经平台、经 HTTP 与 SDK。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import taifeng
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
    # CancelTurn 自己那个 submission：平台不知道它的标识，也不为它登记事件流、不算在途；
    # 内核为它发的只有引擎日志（不看日志文案，只看 submission 与事件类型）
    ours = {turn.submission_id, done[-1].submission_id}
    engine = [record for record in records if record.source == "engine"]
    extra = {record.data["submission_id"] for record in engine} - ours
    assert len(extra) == 1
    assert {record.kind for record in engine if record.data["submission_id"] in extra} == {
        "engine_log"
    }
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


async def stop_mid_sample(world: Any, step: Any) -> tuple[Any, list[Any]]:
    """开一个会话，让第一轮卡在采样半路再停掉；返回 ``(会话, 这一轮的事件)``。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="写到一半", wait=asyncio.Event()))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "写")
    events: list[Any] = []
    task = reader(turn, events)
    await wait_until(lambda: any(e.msg.kind == "assistant_text" for e in events))
    await world.platform.sessions.cancel(world.member, session.session_id)
    await asyncio.wait_for(task, 10)
    return session, events


async def usage_of_turn(world: Any, last: Any) -> list[tuple[int, int, int]]:
    """这一轮入账那天的用量：``(轮数, 输入 token, 合计 token)``。按收尾事件的时刻取日期。"""
    day = usage_day(last.timestamp.timestamp())
    usage = await world.platform.store.list_usage("acme", since=day, until=day)
    return [(record.turns, record.input_tokens, record.total_tokens) for record in usage]


async def test_cancelled_turn_is_metered_with_an_estimate(world: Any, step: Any) -> None:
    """被停的一轮照样记一轮；被打断的那次采样内核报不出 token，按上下文估算计入输入。

    provider 对被打断的请求照样按 prompt 收费，计 0 会被用来绕过当天的 token 配额。
    """
    session, events = await stop_mid_sample(world, step)
    assert events[-1].msg.data["usage"]["total_tokens"] == 0
    records = await world.platform.store.query_audit(
        "acme", AuditQuery(session_id=session.session_id, kind="usage_estimated", limit=10)
    )
    assert [record.data["reason"] for record in records] == ["cancelled"]
    assert records[0].data["submission_id"] == events[-1].submission_id
    estimated = records[0].data["estimated_input_tokens"]
    assert estimated > 0
    assert await usage_of_turn(world, events[-1]) == [(1, estimated, estimated)]


async def test_metering_without_an_estimate_is_logged(
    world: Any, step: Any, monkeypatch: Any, caplog: Any
) -> None:
    """拿不到估算：记警告、审计里留痕，按 0 入账，这一轮照常收尾。"""

    def broken(self: Any) -> dict[str, Any]:
        """模拟内核的快照不可用。"""
        raise RuntimeError("introspect 不可用")

    monkeypatch.setattr("taifeng.AgentEngine.introspect", broken)
    monkeypatch.setattr("xuanpu_server.runtime.cancellation.ADMISSION_WAIT_SECONDS", 0.0)
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="写到一半", wait=asyncio.Event()))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "写")
    events: list[Any] = []
    task = reader(turn, events)
    await wait_until(lambda: any(e.msg.kind == "assistant_text" for e in events))
    live = world.platform.sessions._live[("acme", session.session_id)]  # noqa: SLF001
    # introspect 坏了，平台确认不了受理：直接向内核提交取消，只为走到入账这一步
    await live.engine.submit(taifeng.CancelTurn(submission_id=turn.submission_id))
    with caplog.at_level("WARNING", logger="xuanpu_server.runtime.events"):
        await asyncio.wait_for(task, 10)
    assert events[-1].msg.data["end_reason"] == "cancelled"
    assert "估算" in caplog.text
    records = await world.platform.store.query_audit(
        "acme", AuditQuery(session_id=session.session_id, kind="usage_estimated", limit=10)
    )
    assert [record.data["estimated_input_tokens"] for record in records] == [0]
    assert records[0].data["error"] == "RuntimeError"
    assert await usage_of_turn(world, events[-1]) == [(1, 0, 0)]


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


async def test_cancel_right_after_sending(world: Any, step: Any, read_turn: Any) -> None:
    """发完消息立刻取消（中间没有让出点）也能停下：等内核受理了这一轮再提交取消。

    会话的第一轮（引擎刚打开）与之后的一轮各试一次。
    """
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    for text in ("第一轮", "第二轮"):
        world.models.script(step(text=text, wait=asyncio.Event()))
        turn = await world.platform.sessions.send_message(world.member, session.session_id, text)
        cancelled = await world.platform.sessions.cancel(world.member, session.session_id)
        assert cancelled == [turn.submission_id]
        events = await read_turn(turn, timeout=10)
        assert events[-1].msg.kind == "turn_completed"
        assert events[-1].msg.data["end_reason"] == "cancelled"
    assert world.platform.sessions.in_flight_turns("acme") == 0


async def test_cancel_the_resumed_turn(world: Any, step: Any, read_turn: Any) -> None:
    """审批通过之后续跑的那一轮也能停下，停在它的事件流上。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    gate = asyncio.Event()
    world.models.script(
        step(text="要更新", calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]),
        step(text="续跑到一半", wait=gate),
    )
    suspended = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "更新")
    )
    request_id = suspended[-1].msg.data["pending"][0]["request_id"]
    resumed = await world.platform.sessions.resume(
        world.member, session.session_id, {request_id: {"granted": True}}
    )
    # 送回审批之后立刻取消：同样要等内核受理了这次续跑
    cancelled = await world.platform.sessions.cancel(world.member, session.session_id)
    assert cancelled == [resumed.submission_id]
    events = await read_turn(resumed, timeout=10)
    assert events[-1].msg.kind == "turn_completed"
    assert events[-1].msg.data["end_reason"] == "cancelled"
    assert not gate.is_set()


def lag_suspensions(world: Any, session_id: str, seconds: float) -> None:
    """让会话的事件枢纽处理 ``turn_suspended`` 时落后一段时间（模拟内核已挂起、平台还不知道）。"""
    hub = world.platform.sessions._live[("acme", session_id)].hub  # noqa: SLF001
    original = hub._handle  # noqa: SLF001

    async def lagging(envelope: Any) -> None:
        """遇到挂起事件先等一会儿再照常处理。"""
        if envelope.event.msg.kind == "turn_suspended":
            await asyncio.sleep(seconds)
        await original(envelope)

    hub._handle = lagging  # noqa: SLF001


async def suspend_with_lagging_hub(world: Any, step: Any, read_turn: Any, lag: float) -> Any:
    """开一个会话、热身一轮，再发一条会挂起等审批的消息；返回 ``(会话, 这一轮)``。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="热身"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "热身")
    )
    lag_suspensions(world, session.session_id, lag)
    world.models.script(
        step(text="要更新", calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]),
        step(text="工单已更新"),
    )
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "更新")
    # 内核已经挂起（离开了它的在飞表），事件枢纽还没处理挂起事件
    await wait_until(lambda: bool(world.models.requests) and len(world.models.requests) >= 2)
    await asyncio.sleep(0.05)
    return session, turn


async def approve_and_finish(world: Any, session_id: str, read_turn: Any) -> None:
    """待办还在：批准它，续跑的事件流照常收尾，工具真的调到了。"""
    pending = await world.platform.sessions.pending(world.member, session_id)
    assert len(pending) == 1
    done = await read_turn(
        await world.platform.sessions.resume(
            world.member, session_id, {pending[0]["request_id"]: {"granted": True}}
        ),
        timeout=10,
    )
    assert done[-1].msg.kind == "turn_completed"
    assert [call["arguments"] for call in world.calls()] == [{"id": "T-1"}]


async def test_cancel_skips_a_turn_that_just_suspended(
    world: Any, step: Any, read_turn: Any
) -> None:
    """内核已挂起、平台还没处理挂起事件时取消：不提交取消（否则内核会清掉挂起，平台的待办却还在）。"""
    session, turn = await suspend_with_lagging_hub(world, step, read_turn, lag=0.3)
    assert world.platform.sessions.in_flight_turns("acme") == 1
    assert await world.platform.sessions.cancel(world.member, session.session_id) == []
    events = await read_turn(turn)
    assert events[-1].msg.kind == "turn_suspended"
    assert "turn_cancel_requested" not in await audit_kinds(world, session.session_id)
    await approve_and_finish(world, session.session_id, read_turn)


async def test_cancel_gives_up_when_the_kernel_never_admits(
    world: Any, step: Any, read_turn: Any, monkeypatch: Any, caplog: Any
) -> None:
    """等内核受理有上限：到点仍没受理就跳过、记警告，返回空列表。"""
    monkeypatch.setattr("xuanpu_server.runtime.cancellation.ADMISSION_WAIT_SECONDS", 0.1)
    session, turn = await suspend_with_lagging_hub(world, step, read_turn, lag=0.6)
    with caplog.at_level("WARNING", logger="xuanpu_server.runtime.cancellation"):
        assert await world.platform.sessions.cancel(world.member, session.session_id) == []
    assert turn.submission_id in caplog.text
    assert world.platform.sessions.in_flight_turns("acme") == 1
    events = await read_turn(turn)
    assert events[-1].msg.kind == "turn_suspended"
    await approve_and_finish(world, session.session_id, read_turn)


async def test_cancel_over_http_rejects_busy_and_bad_requests(world: Any) -> None:
    """经 HTTP：别的实例持有是 409 session_busy（带 owner）；submission 标识为空或超长是 400，
    超长输入不回显。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    transport = httpx.ASGITransport(app=create_app(world.platform))
    path = f"/v1/sessions/{session.session_id}/cancel"
    auth = {"Authorization": f"Bearer {world.member_key}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as raw:
        for submission_id in ("", "x" * 100_000):
            response = await raw.post(path, headers=auth, json={"submission_id": submission_id})
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"
            assert len(response.text) < 1_000

        await world.platform.store.acquire_lease(
            "acme", session.session_id, owner="inst-other", now=time.time(), ttl_seconds=60
        )
        response = await raw.post(path, headers=auth, json={})
    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["code"], error["owner"]) == ("session_busy", "inst-other")
