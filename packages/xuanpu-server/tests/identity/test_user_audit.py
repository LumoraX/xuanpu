"""审计记录带上员工：会话里的一切记录归会话主人，与会话无关的控制面操作归调用者；``GET /v1/audit``
按 ``user_id``、``actor`` 过滤。

会话里的记录——内核事件（含平台补的 ``turn_text``、``usage_estimated``）、过闸记录（闸门写的、经
线程兜底归属的，以及人工拒绝的补记）、会话上的控制面操作（建会话、发消息、送审批结果、停止生成、
关闭会话）——``user_id`` 都是会话主人，不论是谁触发的；``actor`` 仍是触发者。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
import taifeng
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.runtime.connectors import ConnectorGate
from xuanpu_server.runtime.events import SessionRef, SubmissionIndex
from xuanpu_server.runtime.gate_audit import ConnectorToolInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.tenancy import Principal

WRITE = {"X-Requested-By": "xuanpu"}


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
async def ident(identity_world: Any) -> AsyncIterator[Any]:
    """配了 ``public_url`` 的平台，样例业务包已发布并生效。"""
    async with identity_world() as item:
        await item.world.publish()
        yield item


async def employee(ident: Any, **fields: Any) -> tuple[Any, Principal]:
    """建一名员工并让他浏览器登录，返回 ``(员工, 他的调用者身份)``。"""
    user = await ident.user(**fields)
    who: Principal = await ident.world.platform.identity.authenticate_login(await ident.login(user))
    return user, who


async def session_records(ident: Any, session_id: str) -> list[AuditRecord]:
    """会话的全部审计记录，按写入先后。"""
    records: list[AuditRecord] = await ident.world.platform.store.query_audit(
        "acme", AuditQuery(session_id=session_id, limit=1000)
    )
    return records


async def wait_until(condition: Callable[[], bool], *, seconds: float = 10.0) -> None:
    """轮询直到条件成立；超时就让用例失败。"""
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"等了 {seconds} 秒条件仍不成立")
        await asyncio.sleep(0.01)


async def test_session_records_belong_to_the_owner(ident: Any, step: Any, read_turn: Any) -> None:
    """员工的会话里，不论是谁触发的，记录都归这名员工：内核事件、闸门放行的与人工拒绝补记的过闸
    记录，以及会话上的控制面操作。管理员的 key 在这个会话里批准、拒绝、发消息、关闭，``actor`` 是
    这把 key，``user_id`` 仍是会话主人。"""
    alice, who = await employee(ident, name="爱丽丝")
    admin = replace(ident.world.admin, user_id=alice.user_id)
    sessions = ident.world.platform.sessions
    session = await sessions.create(who, package="support-desk")
    ident.world.models.script(
        step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]),
        step(text="改好了"),
        step(calls=[("c2", "mcp__crm__update_ticket", {"id": "T-2"})]),
        step(text="不改了"),
        step(text="管理员来过"),
    )
    await read_turn(await sessions.send_message(who, session.session_id, "改 T-1"))
    [first] = await sessions.pending(who, session.session_id)
    approve = {first["request_id"]: {"granted": True}}
    await read_turn(await sessions.resume(admin, session.session_id, approve))
    await read_turn(await sessions.send_message(who, session.session_id, "改 T-2"))
    [second] = await sessions.pending(who, session.session_id)
    # 管理员的 key 来拒绝：补记若取调用者，这里就会是空
    reject = {second["request_id"]: {"granted": False}}
    await read_turn(await sessions.resume(admin, session.session_id, reject))
    await read_turn(await sessions.send_message(admin, session.session_id, "管理员说一句"))
    await sessions.close(admin, session.session_id)

    records = await session_records(ident, session.session_id)
    engine = [record for record in records if record.source == "engine"]
    assert {"turn_suspended", "turn_completed", "turn_text"} <= {record.kind for record in engine}
    assert {record.user_id for record in engine} == {alice.user_id}
    gate = [
        (record.data["decision"], record.data["detail"], record.user_id)
        for record in records
        if record.source == "gate"
    ]
    assert gate == [("allowed", "", alice.user_id), ("rejected", "rejected_by_user", alice.user_id)]
    control = [
        (record.kind, record.actor, record.user_id)
        for record in records
        if record.source == "control" and record.kind != "operation_settled"
    ]
    assert control == [
        ("session_created", who.actor, alice.user_id),
        ("message_sent", who.actor, alice.user_id),
        ("suspension_resolved_by", admin.actor, alice.user_id),
        ("message_sent", who.actor, alice.user_id),
        ("suspension_resolved_by", admin.actor, alice.user_id),
        ("message_sent", admin.actor, alice.user_id),
        ("session_closed", admin.actor, alice.user_id),
    ]


async def test_gate_record_found_by_thread_belongs_to_the_owner(
    ident: Any, step: Any, read_turn: Any
) -> None:
    """内核自己发起的重跑（如到期裁决）不带 submission 标识，闸门按线程找会话：打开会话时登记线程的
    同时登记了主人，这样的过闸记录也归会话主人。"""
    alice, who = await employee(ident, name="爱丽丝")
    sessions = ident.world.platform.sessions
    session = await sessions.create(who, package="support-desk")
    ident.world.models.script(step(text="你好"))
    await read_turn(await sessions.send_message(who, session.session_id, "你好"))
    thread_id = sessions._live[("acme", session.session_id)].engine.thread_id  # noqa: SLF001

    async def invoke(args: dict[str, Any]) -> tuple[str, bool]:
        """连接器照常返回。"""
        return "ok", False

    gate = ConnectorGate(
        invoke=invoke,
        info=ConnectorToolInfo(
            connector="crm", transport="stdio", tool="search_customer", effect="read"
        ),
        full_name="mcp__crm__search_customer",
        tenant_id="acme",
        policy=taifeng.PermissionPolicy(default_mode="allow"),
        audit=ident.world.platform.audit,
        submissions=sessions._submissions,  # noqa: SLF001 —— 用平台自己的对照表
        timeout_seconds=5,
    )
    context = taifeng.ToolContext(
        call_id="c-rerun", cancel=taifeng.CancellationToken(), thread_id=thread_id, extras={}
    )
    await gate({"name": "张三"}, context)
    [record] = await ident.world.platform.store.query_audit(
        "acme", AuditQuery(source="gate", session_id=session.session_id)
    )
    assert (record.data["call_id"], record.data["submission_id"]) == ("c-rerun", None)
    assert record.user_id == alice.user_id


async def test_stop_and_estimate_belong_to_the_owner(ident: Any, step: Any) -> None:
    """管理员的 key 停下员工会话里的一轮：``turn_cancel_requested`` 的 ``actor`` 是这把 key，
    ``user_id`` 是会话主人；平台补的 ``usage_estimated`` 也归主人。"""
    alice, who = await employee(ident, name="爱丽丝")
    sessions = ident.world.platform.sessions
    session = await sessions.create(who, package="support-desk")
    ident.world.models.script(step(text="写到一半", wait=asyncio.Event()))
    turn = await sessions.send_message(who, session.session_id, "写")
    events: list[Any] = []

    async def read() -> None:
        """读到这一轮结束。"""
        async for event in turn.stream.events():
            events.append(event)

    task = asyncio.ensure_future(read())
    await wait_until(lambda: any(event.msg.kind == "assistant_text" for event in events))
    assert await sessions.cancel(ident.world.admin, session.session_id) == [turn.submission_id]
    await asyncio.wait_for(task, 10)

    records = {record.kind: record for record in await session_records(ident, session.session_id)}
    requested = records["turn_cancel_requested"]
    assert (requested.actor, requested.user_id) == (ident.world.admin.actor, alice.user_id)
    estimated = records["usage_estimated"]
    assert (estimated.source, estimated.user_id) == ("engine", alice.user_id)


async def test_key_sessions_have_no_owner_in_audit(ident: Any, step: Any, read_turn: Any) -> None:
    """API key 直接建的会话没有主人：会话内的记录 ``user_id`` 为空。"""
    sessions = ident.world.platform.sessions
    session = await sessions.create(ident.world.member, package="support-desk")
    ident.world.models.script(step(text="你好"))
    await read_turn(await sessions.send_message(ident.world.member, session.session_id, "你好"))
    records = await session_records(ident, session.session_id)
    assert {"turn_text", "message_sent"} <= {record.kind for record in records}
    assert {record.user_id for record in records} == {None}


async def test_audit_query_filters_by_user(ident: Any, step: Any, read_turn: Any) -> None:
    """``GET /v1/audit?user_id=`` 只返回与这名员工有关的记录，记录里带 ``user_id``；仍只对管理员
    开放。"""
    alice, alice_who = await employee(ident, name="爱丽丝")
    bob, bob_who = await employee(ident, name="鲍勃")
    sessions = ident.world.platform.sessions
    ident.world.models.script(step(text="给爱丽丝"), step(text="给鲍勃"))
    for who in (alice_who, bob_who):
        session = await sessions.create(who, package="support-desk")
        await read_turn(await sessions.send_message(who, session.session_id, "你好"))

    admin = bearer(ident.world.admin_key)
    params = {"user_id": alice.user_id, "limit": "1000"}
    response = await ident.http.get("/v1/audit", headers=admin, params=params)
    assert response.status_code == 200, response.text
    records = response.json()["records"]
    assert {record["user_id"] for record in records} == {alice.user_id}
    assert {record["source"] for record in records} == {"control", "engine"}
    assert any(record["kind"] == "turn_text" for record in records)
    assert all("data" not in record for record in records)

    everything = await ident.http.get("/v1/audit", headers=admin, params={"limit": "1000"})
    owners = {record["user_id"] for record in everything.json()["records"]}
    assert owners == {None, alice.user_id, bob.user_id}

    member_cookie = {**ident.cookie(await ident.login(alice)), **WRITE}
    for headers in (member_cookie, bearer(ident.world.member_key)):
        denied = await ident.http.get("/v1/audit", headers=headers, params=params)
        assert (denied.status_code, denied.json()["error"]["code"]) == (403, "permission_denied")


async def test_audit_query_filters_by_actor(ident: Any, step: Any, read_turn: Any) -> None:
    """``GET /v1/audit?actor=``：查某个管理员在别人会话里做了什么——记录归会话主人，``actor``
    是他。"""
    alice, alice_who = await employee(ident, name="爱丽丝")
    delegated = replace(ident.world.admin, user_id=alice.user_id)
    sessions = ident.world.platform.sessions
    session = await sessions.create(alice_who, package="support-desk")
    ident.world.models.script(step(text="爱丽丝的"), step(text="管理员的"))
    await read_turn(await sessions.send_message(alice_who, session.session_id, "你好"))
    await read_turn(await sessions.send_message(delegated, session.session_id, "我来"))
    await sessions.close(delegated, session.session_id)

    params = {"actor": delegated.actor, "user_id": alice.user_id}
    response = await ident.http.get(
        "/v1/audit", headers=bearer(ident.world.admin_key), params=params
    )
    assert response.status_code == 200, response.text
    records = response.json()["records"]
    assert [record["kind"] for record in records] == ["message_sent", "session_closed"]
    assert {(record["actor"], record["user_id"]) for record in records} == {
        (delegated.actor, alice.user_id)
    }
    assert all("data" not in record for record in records)


def test_submission_index_remembers_the_owner() -> None:
    """对照表按 submission、再按线程查到会话与它的主人；查不到时两项都为空。"""
    index = SubmissionIndex(capacity=2)
    index.register("sub_1", "ses_1", "usr_1")
    index.register_thread("thr_1", "ses_1", "usr_1")
    index.register("sub_2", "ses_2", None)
    assert index.session_of("sub_1") == SessionRef("ses_1", "usr_1")
    assert index.session_of(None, "thr_1") == SessionRef("ses_1", "usr_1")
    assert index.session_of("sub_2", "thr_1") == SessionRef("ses_2", None)
    assert index.session_of("sub_nope") == SessionRef(None, None)
    index.register("sub_3", "ses_3", "usr_3")
    # 超出上限淘汰最早登记的
    assert index.session_of("sub_1") == SessionRef(None, None)
