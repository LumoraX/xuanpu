"""平台主流程：发布业务包 → 建会话 → 对话 → 过闸 → 审计与计量。

模型是脚本化的，其余都是真的：内核引擎、沙盒里的脚本、MCP 连接器进程。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.quota import usage_day

if TYPE_CHECKING:
    import taifeng

pytestmark = pytest.mark.usefixtures("world")


def kinds(events: list[taifeng.EventMsg]) -> list[str]:
    """事件类型序列。"""
    return [event.msg.kind for event in events]


def text_of(events: list[taifeng.EventMsg]) -> str:
    """把流式文本拼起来。"""
    return "".join(
        event.msg.data.get("delta", "") for event in events if event.msg.kind == "assistant_text"
    )


async def audit_kinds(world: Any, **query: Any) -> list[str]:
    """租户 acme 的审计事件类型序列。"""
    records = await world.platform.store.query_audit("acme", AuditQuery(limit=1000, **query))
    return [record.kind for record in records]


async def test_plain_conversation(world: Any, step: Any, read_turn: Any) -> None:
    """最简单的一轮对话：事件流完整，用量入账，审计有头有尾。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    assert (session.version, session.entry, session.status) == ("1.0.0", "triage", "active")

    world.models.script(step(text="您好，请问有什么可以帮您？"))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    events = await read_turn(turn)

    assert kinds(events)[0] == "turn_started"
    assert kinds(events)[-1] == "turn_completed"
    assert text_of(events) == "您好，请问有什么可以帮您？"
    assert world.models.created_for == [("acme", "balanced")]

    stored = await world.platform.sessions.get(world.member, session.session_id)
    assert stored.thread_id is not None
    assert stored.owner == "inst-test"

    usage = await world.platform.store.get_usage("acme", usage_day(stored.updated_at))
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.turns) == (
        100, 20, 120, 1,
    )  # fmt: skip

    recorded = await audit_kinds(world, session_id=session.session_id)
    assert recorded[0] == "session_created"
    assert "turn_started" in recorded
    assert recorded[-2:] == ["turn_completed", "turn_text"]
    # 流式增量不进审计
    assert "assistant_text" not in recorded


async def test_conversation_keeps_context_across_turns(
    world: Any, step: Any, read_turn: Any
) -> None:
    """同一个会话的第二轮对话能看到第一轮的内容。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="订单号是 A-1024"), step(text="好的"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查订单")
    )
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "谢谢")
    )
    assert "订单号是 A-1024" in world.models.last_prompt()
    assert "查订单" in world.models.last_prompt()


async def test_read_connector_runs_without_approval(world: Any, step: Any, read_turn: Any) -> None:
    """标准审批强度下，只读的连接器工具直接放行；凭据注入进连接器进程，宿主环境不泄漏。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__search_customer", {"name": "张三"})]),
        step(text="查到了"),
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查张三")
    )
    assert kinds(events)[-1] == "turn_completed"
    assert world.calls() == [{"name": "search_customer", "arguments": {"name": "张三"}}]

    completed = next(e for e in events if e.msg.kind == "tool_call_completed")
    output = json.loads(completed.msg.data["output"])
    assert output["region"] == "cn"
    assert output["token_length"] == len("s3cret-token-value")
    assert output["inherited"] == []


async def test_gate_audit_has_digest_not_arguments_or_secrets(
    world: Any, step: Any, read_turn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """过闸记录只含参数摘要；整份审计里找不到密钥明文，也找不到宿主环境里的值。"""
    monkeypatch.setenv("HOST_ONLY_SECRET", "host-leak-value")
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__search_customer", {"name": "张三"})]),
        step(text="查到了"),
    )
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查张三")
    )
    records = await world.platform.store.query_audit("acme", AuditQuery(limit=1000))
    gate = [record for record in records if record.source == "gate"]
    assert len(gate) == 1
    assert gate[0].kind == "connector_call"
    assert gate[0].data["connector"] == "crm"
    assert gate[0].data["tool"] == "search_customer"
    assert gate[0].data["effect"] == "read"
    assert gate[0].data["decision"] == "allowed"
    assert gate[0].data["is_error"] is False
    assert gate[0].data["arguments_digest"].startswith("sha256:")
    assert "arguments" not in gate[0].data

    everything = json.dumps([record.data for record in records], ensure_ascii=False)
    assert "s3cret-token-value" not in everything
    assert "host-leak-value" not in everything


async def test_write_connector_suspends_until_approved(
    world: Any, step: Any, read_turn: Any
) -> None:
    """写操作要审批：本轮对话挂起，工具没有被调用；批准后续跑，工具才执行。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(text="需要更新工单", calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]),
        step(text="工单已更新"),
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "更新工单")
    )
    assert kinds(events)[-1] == "turn_suspended"
    assert world.calls() == []

    pending = events[-1].msg.data["pending"]
    assert len(pending) == 1
    assert pending[0]["reason"] == "permission"
    assert pending[0]["detail"]["target"] == "mcp__crm__update_ticket"
    assert pending[0]["detail"]["scope"] == "tool_use"

    resumed = await read_turn(
        await world.platform.sessions.resume(
            world.member, session.session_id, {pending[0]["request_id"]: {"granted": True}}
        )
    )
    assert kinds(resumed)[-1] == "turn_completed"
    assert text_of(resumed) == "工单已更新"
    assert world.calls() == [{"name": "update_ticket", "arguments": {"id": "T-1"}}]

    recorded = await audit_kinds(world, session_id=session.session_id)
    assert "turn_suspended" in recorded
    assert "suspension_resolved_by" in recorded
    assert "connector_call" in recorded


async def test_rejected_approval_never_reaches_the_connector(
    world: Any, step: Any, read_turn: Any
) -> None:
    """审批被拒：工具始终没有执行，模型拿到的是被拒的结果。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]),
        step(text="好的，不更新了"),
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "更新工单")
    )
    request_id = events[-1].msg.data["pending"][0]["request_id"]
    resumed = await read_turn(
        await world.platform.sessions.resume(
            world.member,
            session.session_id,
            {request_id: {"granted": False, "reason": "未经客户确认"}},
        )
    )
    assert kinds(resumed)[-1] == "turn_completed"
    assert world.calls() == []
    assert "未经客户确认" in world.models.last_prompt()


async def test_strict_level_asks_even_for_reads(world: Any, step: Any, read_turn: Any) -> None:
    """严格审批强度下只读工具也要审批。"""
    await world.publish(approval="strict")
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(calls=[("c1", "mcp__crm__search_customer", {"name": "张三"})]))
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查张三")
    )
    assert kinds(events)[-1] == "turn_suspended"
    assert world.calls() == []


async def test_tool_outside_allowlist_is_not_exposed(world: Any, step: Any, read_turn: Any) -> None:
    """连接器提供了、但清单白名单里没有的工具，模型调不到。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(calls=[("c1", "mcp__crm__hidden_tool", {})]), step(text="调不了"))
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "试试")
    )
    assert kinds(events)[-1] == "turn_completed"
    assert world.calls() == []
    offered = {tool["name"] for tool in world.models.requests[0].model_dump()["tools"]}
    assert "mcp__crm__search_customer" in offered
    assert "mcp__crm__hidden_tool" not in offered


async def test_script_runs_inside_sandbox(world: Any, step: Any, read_turn: Any) -> None:
    """skill 脚本在沙盒里运行：只能写工作区。"""
    await world.publish()
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
        step(text="已盖章"),
    )
    events = await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "盖章")
    )
    assert kinds(events)[-1] == "turn_completed"
    completed = next(e for e in events if e.msg.kind == "tool_call_completed")
    assert "stamped ok" in completed.msg.data["output"], completed.msg.data
