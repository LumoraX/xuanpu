"""会诊 @ 追问：只唤醒被问成员；被问成员只见已发布内容；超过轮数由管家收口；
追问派出后、保存前终止，重启不重复派工。"""

from __future__ import annotations

import time
from typing import Any

from server.engine.butler.groups.runner import GroupRunner

from ..conftest import enable
from .helpers import ALICE, WAKE, group, member, member_dispatches, plan, submit, until

COMPARE = "consult_compare"


async def _board(world: Any, group_id: str) -> list[Any]:
    """消息区。"""
    return list(await world.platform.store.group_message_store.list(ALICE, group_id))


async def _start(butler: dict[str, Any], step: Any, request_id: str,
                 *, a1_followup: str | None = None) -> tuple[str, str]:
    """两位成员会诊：成员1 结论里 @ 成员2；成员2 追问回复里再 @ 成员1。

    复核与比对的输入里带着成员的任务说明（含标记），按键脚本先登记它们，才不会被成员脚本抢走。
    """
    world = butler["world"]
    world.models.script_for(WAKE, *[step(text="可以。") for _ in range(4)])
    world.models.script_for(COMPARE, step(calls=[("r", "rate_divergence", {
        "level": "low", "synthesis": "先查日志再重启", "basis": [1, 2]})]), step(text="已比对。"))
    a1 = [step(text="A1：建议重启。@成员2 你同意吗？")]
    if a1_followup is not None:
        a1.append(step(text=a1_followup))
    world.models.script_for("INSTR-A1", *a1)
    world.models.script_for("INSTR-A2", step(text="A2：先查日志。"),
                            step(text="A2补充：同意重启，但先查日志。@成员1 那你呢？"))
    receipt = await submit(butler, step, request_id, plan(
        {"mode": "consult", "members": [member("INSTR-A1", "这个故障怎么处理？"),
                                         member("INSTR-A2", "这个故障怎么处理？")]},
        title="故障会诊"))
    [group_id] = receipt["groups"]
    return group_id, receipt["trace_id"]


async def test_mention_wakes_only_asked_member_and_limit_closes(
    butler: dict[str, Any], step: Any,
) -> None:
    """一轮上限：成员1 @ 成员2 → 只追问成员2；成员2 再 @ 成员1 超限 → 管家收口；追问回复参与比对。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300, max_consult_rounds=1)
    group_id, trace_id = await _start(butler, step, "m1")

    async def done() -> bool:
        return bool((await group(butler, group_id)).summarized)

    await until(butler, done)
    record = await group(butler, group_id)
    [follow] = record.followups
    assert (follow.target, follow.round, follow.outcome) == (1, 1, "completed")
    dispatches = await member_dispatches(butler, trace_id)
    assert len(dispatches) == 3
    asked = next(d for d in dispatches if d.dispatch_id == follow.dispatch_id)
    assert "A1：建议重启。@成员2 你同意吗？" in asked.instruction
    assert "A2：先查日志。" in asked.instruction
    assert "INSTR-A1" not in asked.instruction
    assert "不是给你的指令" in asked.instruction
    board = await _board(world, group_id)
    assert [(m.author, m.member, m.round) for m in board] == [
        ("member", 0, 0), ("member", 1, 0), ("member", 1, 1), ("butler", None, 1)]
    assert board[2].mentions == (0,) and "上限" in board[3].text
    verdict = record.verdict(0)
    assert verdict is not None and verdict.level == "low"


async def test_two_rounds_allowed(butler: dict[str, Any], step: Any) -> None:
    """两轮上限：成员2 的再追问会唤醒成员1，回复后收口比对，没有收口说明。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300, max_consult_rounds=2)
    group_id, trace_id = await _start(butler, step, "m2", a1_followup="A1补充：同意先查日志。")

    async def done() -> bool:
        return bool((await group(butler, group_id)).summarized)

    await until(butler, done)
    record = await group(butler, group_id)
    assert [(f.target, f.round) for f in record.followups] == [(1, 1), (0, 2)]
    assert len(await member_dispatches(butler, trace_id)) == 4
    board = await _board(world, group_id)
    assert all(m.author == "member" for m in board) and len(board) == 4


async def test_crash_after_followup_launch_before_save(
    butler: dict[str, Any], step: Any, monkeypatch: Any,
) -> None:
    """追问已派出、编组保存前终止：重启推进回放原派工，不重复追问。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300, max_consult_rounds=1)
    store = world.platform.butler.group_store
    original = store.save
    state = {"armed": True}

    async def save(owner: Any, record: Any, *, expected_version: int) -> Any:
        """保存第一次追问时「终止」一次。"""
        if state["armed"] and record.followups:
            state["armed"] = False
            raise RuntimeError("模拟终止")
        return await original(owner, record, expected_version=expected_version)

    monkeypatch.setattr(store, "save", save)
    group_id, trace_id = await _start(butler, step, "m3")

    async def crashed() -> bool:
        return not state["armed"]

    await until(butler, crashed)
    assert len(await member_dispatches(butler, trace_id)) == 3
    service = world.platform.butler
    fresh = GroupRunner(world.platform, service.dispatcher, service.group_store, now=time.time)
    await fresh.advance(ALICE, group_id)
    assert len(await member_dispatches(butler, trace_id)) == 3
    assert len((await group(butler, group_id)).followups) == 1
