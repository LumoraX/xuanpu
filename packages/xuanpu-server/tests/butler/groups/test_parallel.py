"""并行：成员各自执行、全部终态后汇总一次；部分失败如实汇报；成员撤权只拒该成员。"""

from __future__ import annotations

from typing import Any

from xuanpu_server.errors import NotFoundError

from ..conftest import enable
from .helpers import ALICE, WAKE, group, member, member_dispatches, plan, submit, summaries, until


async def test_parallel_members_then_one_summary(butler: dict[str, Any], step: Any) -> None:
    """两位成员同时启动、互不等待；都完成后只汇总一次，成员不单独交付；树形正确。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for("INSTR-P1", step(text="P1：工单 T-1 已关闭。"))
    world.models.script_for("INSTR-P2", step(text="P2：工单 T-2 待处理。"))
    world.models.script_for(WAKE, step(text="结果回答了原请求。"), step(text="结果回答了原请求。"))
    receipt = await submit(butler, step, "p1", plan(
        {"mode": "parallel", "members": [member("INSTR-P1"), member("INSTR-P2")]}))
    [group_id] = receipt["groups"]
    launched = await member_dispatches(butler, receipt["trace_id"])
    assert len(launched) == 2 and all(d.state == "running" for d in launched)

    async def done() -> bool:
        return (await group(butler, group_id)).summarized

    await until(butler, done)
    record = await group(butler, group_id)
    assert record.state == "completed" and record.step_outcomes == ("completed",)
    [message] = await summaries(butler)
    assert "P1：工单 T-1 已关闭" in message["text"] and "P2：工单 T-2 待处理" in message["text"]
    individual = [m for m in await world.platform.workspace.messages(
        butler["alice"], limit=100, latest=True, private_only=True)
        if str(m["request_id"]).endswith(":deliver")]
    assert individual == []
    for _ in range(3):
        await world.platform.butler.wakeup.run_once()
    assert len(await summaries(butler)) == 1
    view = await world.platform.store.trace_store.get(ALICE, receipt["trace_id"])
    [group_span] = [s for s in view.spans if s.kind == "group"]
    dispatch_spans = [s for s in view.spans if s.kind == "dispatch"]
    assert len(dispatch_spans) == 2
    assert all(s.parent_span_id == group_span.span_id for s in dispatch_spans)
    assert not view.trace.incomplete


async def test_revoked_member_is_refused_others_continue(
    butler: dict[str, Any], step: Any, monkeypatch: Any,
) -> None:
    """第二步的一位成员在启动前失去资格：只拒该成员，另一位照常完成；整体如实标失败。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for("INSTR-R1", step(text="第一步完成。"))
    world.models.script_for("INSTR-R2", step(text="第二步完成。"))
    world.models.script_for(WAKE, *[step(text="可以。") for _ in range(3)])
    receipt = await submit(butler, step, "r1", plan(
        {"mode": "parallel", "members": [member("INSTR-R1")]},
        {"mode": "parallel", "members": [member("INSTR-R2"), member("INSTR-RX")]}))
    [group_id] = receipt["groups"]
    resolver = world.platform.artisans
    original = resolver.resolve

    async def revoked(who: Any, package: str, entry: str) -> Any:
        """模拟撤权：之后的 INSTR-RX 成员启动时不可用（按成员顺序第二个）。"""
        record = await group(butler, group_id)
        if record.step == 1 and record.run(1, 0) is not None:
            raise NotFoundError("匠人不可用")
        return await original(who, package, entry)

    monkeypatch.setattr(resolver, "resolve", revoked)

    async def done() -> bool:
        return (await group(butler, group_id)).summarized

    await until(butler, done)
    record = await group(butler, group_id)
    assert record.state == "failed"
    assert record.run(1, 0).outcome == "completed"
    assert (record.run(1, 1).outcome, record.run(1, 1).end_reason) == ("failed",
                                                                        "member_unavailable")
    [message] = await summaries(butler)
    assert "第二步完成" in message["text"] and "已不可用" in message["text"]
    assert "不会自动重跑" in message["text"]
