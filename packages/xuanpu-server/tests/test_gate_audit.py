"""过闸审计补齐：审批后执行的调用归到 resume 那一轮；人工拒绝的连接器调用补一条过闸记录。

远端 MCP 与 HTTP 连接器的拒绝分别在 ``test_remote_mcp.py``、``test_http_connector.py`` 里
（假服务的夹具在那两个模块里，测试模块之间不能互相 import）。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from xuanpu_server.audit import AuditLog, digest_arguments
from xuanpu_server.controlplane import AuditQuery, AuditRecord, InMemoryControlPlane
from xuanpu_server.runtime.gate_audit import ConnectorToolInfo, RejectionAudit

if TYPE_CHECKING:
    import pytest


async def gate_records(world: Any) -> list[Any]:
    """全部过闸记录。"""
    return list(
        await world.platform.store.query_audit("acme", AuditQuery(source="gate", limit=100))
    )


async def suspend(
    world: Any, step: Any, read_turn: Any, calls: list[tuple[str, str, dict[str, Any]]]
) -> tuple[Any, Any, list[dict[str, Any]]]:
    """发布样例业务包、开会话，让模型发起给定的调用并停在审批点。

    Returns:
        会话、挂起的那一轮、当前待办。
    """
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(calls=calls), step(text="好了"))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "改")
    events = await read_turn(turn)
    assert events[-1].msg.kind == "turn_suspended"
    return session, turn, await world.platform.sessions.pending(world.member, session.session_id)


async def test_approved_call_is_attributed_to_the_resume_submission(
    world: Any, step: Any, read_turn: Any
) -> None:
    """审批后执行的连接器调用，过闸记录带触发重跑的那次 resume 的提交标识。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="好了")
    )
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "改")
    )
    request_id = (await world.platform.sessions.pending(world.member, session.session_id))[0][
        "request_id"
    ]
    resumed = await world.platform.sessions.resume(
        world.member, session.session_id, {request_id: {"granted": True}}
    )
    await read_turn(resumed)
    gate = await world.platform.store.query_audit("acme", AuditQuery(source="gate", limit=100))
    assert [r.data["submission_id"] for r in gate if r.data["decision"] == "allowed"] == [
        resumed.submission_id
    ]
    assert [r.session_id for r in gate] == [session.session_id]


async def test_rejected_call_leaves_a_gate_record(world: Any, step: Any, read_turn: Any) -> None:
    """人工拒绝的连接器调用补一条过闸记录：归到挂起的那一轮，参数只记摘要，工具没有被调用。"""
    arguments = {"id": "T-1", "note": "客户的机密备注"}
    session, suspended, pending = await suspend(
        world, step, read_turn, [("c1", "mcp__crm__update_ticket", arguments)]
    )
    resumed = await world.platform.sessions.resume(
        world.member, session.session_id, {pending[0]["request_id"]: {"granted": False}}
    )
    events = await read_turn(resumed)
    assert events[-1].msg.kind == "turn_completed"
    assert world.calls() == []

    [record] = await gate_records(world)
    assert (record.kind, record.session_id, record.actor) == (
        "connector_call", session.session_id, "engine",
    )  # fmt: skip
    data = record.data
    assert (data["decision"], data["detail"], data["is_error"]) == (
        "rejected", "rejected_by_user", False,
    )  # fmt: skip
    assert (data["connector"], data["transport"], data["tool"], data["effect"]) == (
        "crm", "stdio", "update_ticket", "write",
    )  # fmt: skip
    assert (data["call_id"], data["thread_id"]) == ("c1", pending[0]["detail"]["thread_id"])
    assert data["submission_id"] == suspended.submission_id
    assert data["submission_id"] != resumed.submission_id
    assert data["duration_ms"] == 0
    assert data["arguments_digest"] == digest_arguments(arguments)
    assert "args" not in data and "arguments" not in data
    assert "机密备注" not in json.dumps(data, ensure_ascii=False)


async def test_mixed_decisions_only_record_the_rejected_call(
    world: Any, step: Any, read_turn: Any
) -> None:
    """一次挂起两个请求、一个批准一个拒绝：只为被拒的补记录；批准的那个之后过闸、记 allowed。

    两条记录的字段集合一致（共用同一个构造函数）。
    """
    session, suspended, pending = await suspend(
        world, step, read_turn,
        [
            ("c1", "mcp__crm__update_ticket", {"id": "T-1"}),
            ("c2", "mcp__crm__update_ticket", {"id": "T-2"}),
        ],
    )  # fmt: skip
    by_call = {item["detail"]["call_id"]: item["request_id"] for item in pending}
    resumed = await world.platform.sessions.resume(
        world.member,
        session.session_id,
        {by_call["c1"]: {"granted": True}, by_call["c2"]: {"granted": False, "reason": "不改"}},
    )
    events = await read_turn(resumed)
    assert events[-1].msg.kind == "turn_completed"
    assert world.calls() == [{"name": "update_ticket", "arguments": {"id": "T-1"}}]

    gate = {record.data["call_id"]: record for record in await gate_records(world)}
    assert {call_id: r.data["decision"] for call_id, r in gate.items()} == {
        "c1": "allowed", "c2": "rejected",
    }  # fmt: skip
    assert gate["c1"].data["submission_id"] == resumed.submission_id
    assert gate["c2"].data["submission_id"] == suspended.submission_id
    assert gate["c2"].data["detail"] == "rejected_by_user"
    assert set(gate["c1"].data) == set(gate["c2"].data)


async def test_rejecting_a_non_connector_request_leaves_no_gate_record(
    world: Any, step: Any, read_turn: Any
) -> None:
    """严格审批下脚本也要审批：拒绝的是沙盒脚本，不是连接器工具，不补过闸记录。"""
    await world.publish(approval="strict")
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(
            calls=[
                (
                    "c1",
                    "run_script",
                    {"skill_id": "triage", "script_name": "stamp", "args": {"label": "ok"}},
                )
            ]
        ),
        step(text="不盖章了"),
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "盖章")
    )
    assert events[-1].msg.kind == "turn_suspended"
    [item] = await world.platform.sessions.pending(world.member, session.session_id)
    assert item["detail"]["scope"] != "tool_use"

    resumed = await world.platform.sessions.resume(
        world.member, session.session_id, {item["request_id"]: {"granted": False}}
    )
    assert (await read_turn(resumed))[-1].msg.kind == "turn_completed"
    assert await gate_records(world) == []


async def test_repeated_rejection_is_recorded_once(world: Any, step: Any, read_turn: Any) -> None:
    """同一个拒绝送回两次：第二次内核以 suspension_resolve_rejected 拒绝，不再补第二条记录。"""
    session, _, pending = await suspend(
        world, step, read_turn, [("c1", "mcp__crm__update_ticket", {"id": "T-1"})]
    )
    resolutions = {pending[0]["request_id"]: {"granted": False}}
    await read_turn(
        await world.platform.sessions.resume(world.member, session.session_id, resolutions)
    )
    again = await read_turn(
        await world.platform.sessions.resume(world.member, session.session_id, resolutions),
        timeout=10,
    )
    assert [event.msg.kind for event in again] == ["suspension_resolve_rejected"]
    assert [r.data["decision"] for r in await gate_records(world)] == ["rejected"]


async def test_resume_the_kernel_refuses_leaves_no_gate_record(
    world: Any, step: Any, read_turn: Any
) -> None:
    """送回里夹了一个不存在的待办：内核整次拒绝、什么都没核销，被拒的那条也不补记录。

    之后照常送回拒绝，才补上一条。
    """
    session, suspended, pending = await suspend(
        world, step, read_turn, [("c1", "mcp__crm__update_ticket", {"id": "T-1"})]
    )
    request_id = pending[0]["request_id"]
    refused = await read_turn(
        await world.platform.sessions.resume(
            world.member,
            session.session_id,
            {request_id: {"granted": False}, "req_nope": {"granted": True}},
        ),
        timeout=10,
    )
    assert [event.msg.kind for event in refused] == ["suspension_resolve_rejected"]
    assert await gate_records(world) == []
    still = await world.platform.sessions.pending(world.member, session.session_id)
    assert [item["request_id"] for item in still] == [request_id]

    await read_turn(
        await world.platform.sessions.resume(
            world.member, session.session_id, {request_id: {"granted": False}}
        )
    )
    [record] = await gate_records(world)
    assert (record.data["decision"], record.data["submission_id"]) == (
        "rejected", suspended.submission_id,
    )  # fmt: skip


# ---------------------------------------------------------------------------
# 补记本身：喂一条构造的挂起记录与一份连接器登记
# ---------------------------------------------------------------------------

REGISTRY = {
    "mcp__docs__update_doc": ConnectorToolInfo(
        connector="docs", transport="remote", tool="update_doc", effect="write"
    ),
    "mcp__crm__update_ticket": ConnectorToolInfo(
        connector="crm", transport="stdio", tool="update_ticket", effect="write"
    ),
}


def permission(request_id: str, target: str, *, scope: str = "tool_use", **detail: Any) -> Any:
    """一条审批待办，形状照内核 ``turn_suspended`` 事件里的 ``pending`` 项。"""
    fields = {"call_id": f"call-{request_id}", "thread_id": "thr_1", "args": {"id": request_id}}
    return {
        "request_id": request_id,
        "reason": "permission",
        "related_call_id": f"call-{request_id}",
        "detail": {"scope": scope, "target": target, "reason": "审批", **fields, **detail},
    }


async def suspended_store(*pending: Any) -> InMemoryControlPlane:
    """审计里有一次挂起（挂起那一轮是 ``sub_suspended``）的控制面。"""
    store = InMemoryControlPlane()
    record = AuditRecord(
        tenant_id="acme",
        source="engine",
        kind="turn_suspended",
        at=1.0,
        actor="engine",
        session_id="ses_1",
        stream="st_1",
        seq=1,
        data={"submission_id": "sub_suspended", "record_id": "sr_1", "pending": list(pending)},
    )
    await store.append_audit([record])
    return store


async def test_only_connector_tools_are_picked_and_transport_comes_from_the_registry() -> None:
    """表单、非 tool_use 的审批、登记里没有的工具都跳过；连接器工具的传输按登记，不按前缀。"""
    store = await suspended_store(
        permission("r1", "mcp__docs__update_doc"),
        {"request_id": "r2", "reason": "form", "detail": {"target": "mcp__docs__update_doc"}},
        permission("r3", "mcp__docs__update_doc", scope="shell_exec"),
        permission("r4", "mcp__other__tool"),
        permission("r5", "mcp__crm__update_ticket"),
    )
    audit = AuditLog(store)
    rejections = RejectionAudit(store=store, audit=audit)
    resolutions = {
        "r1": {"granted": False},
        "r2": {"answer": "表单答案"},
        "r3": {"granted": False},
        "r4": {"granted": False},
        "r5": {"granted": True},
    }
    pending = await rejections.prepare("acme", "ses_1", resolutions)
    assert [item["request_id"] for item in pending.requests] == ["r1", "r2", "r3", "r4"]
    calls = rejections.calls(pending, REGISTRY.get)
    assert [(c.call_id, c.info.transport, c.submission_id) for c in calls] == [
        ("call-r1", "remote", "sub_suspended")
    ]
    assert await rejections.record(pending, REGISTRY.get) == 1
    [record] = await store.query_audit("acme", AuditQuery(source="gate"))
    assert (record.session_id, record.data["transport"], record.data["decision"]) == (
        "ses_1", "remote", "rejected",
    )  # fmt: skip
    assert rejections.failures == 0


async def test_approvals_alone_do_not_read_the_audit() -> None:
    """送回里只有批准：不读审计（读审计要从头翻一遍会话的事件），也不算失败。"""
    store = InMemoryControlPlane()
    rejections = RejectionAudit(store=store, audit=AuditLog(store))

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("不该读审计")

    store.query_audit = boom  # type: ignore[method-assign]
    pending = await rejections.prepare("acme", "ses_1", {"r1": {"granted": True}})
    assert pending.requests == ()
    assert rejections.failures == 0


async def test_failures_are_logged_and_counted_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """读不到待办、待办缺调用信息、写不进过闸记录：都记 error 日志并计数（经 ``on_failure``
    报出是哪个环节），不抛给送回的一方；一条写失败不影响其余的。"""
    caplog.set_level(logging.INFO)
    store = await suspended_store(
        permission("r1", "mcp__docs__update_doc"),
        permission("r2", "mcp__docs__update_doc", args=None),
        permission("r3", "mcp__crm__update_ticket"),
    )
    written: list[str] = []

    class FlakyAudit:
        """第一条过闸记录写失败，之后的照常写。"""

        async def gate(self, tenant_id: str, kind: str, data: Any, **kwargs: Any) -> None:
            if not written:
                written.append("failed")
                raise RuntimeError("控制面写不进去")
            written.append(data["call_id"])

    stages: list[str] = []
    rejections = RejectionAudit(
        store=store,
        audit=FlakyAudit(),  # type: ignore[arg-type]
        on_failure=stages.append,
    )
    rejected = {"granted": False}
    pending = await rejections.prepare(
        "acme", "ses_1", {"r1": rejected, "r2": rejected, "r3": rejected}
    )
    # r2 缺参数补不上；r1 写失败；r3 照常写进去
    assert await rejections.record(pending, REGISTRY.get) == 1
    assert written == ["failed", "call-r3"]
    assert rejections.failures == 2
    assert stages == ["incomplete", "write"]

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("控制面读不出来")

    store.query_audit = broken  # type: ignore[method-assign]
    assert (await rejections.prepare("acme", "ses_1", {"r1": rejected})).requests == ()
    assert rejections.failures == 3
    assert stages == ["incomplete", "write", "read"]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 3
