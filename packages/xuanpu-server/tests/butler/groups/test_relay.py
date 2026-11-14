"""接力：下一位拿到前一位的成果引用与摘要（标明是数据）；任一步失败即停。"""

from __future__ import annotations

from typing import Any

from ..conftest import enable
from .helpers import WAKE, group, member, member_dispatches, plan, submit, summaries, until


async def test_relay_passes_summary_and_reference(butler: dict[str, Any], step: Any) -> None:
    """第一位完成后才启动第二位；第二位的说明里有第一位的摘要与派工引用。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for("INSTR-L1", step(text="L1：客户 C-9 的工单共 3 张。"))
    world.models.script_for("INSTR-L2", step(text="L2：已整理成表。"))
    world.models.script_for(WAKE, step(text="可以。"), step(text="可以。"))
    receipt = await submit(butler, step, "l1", plan(
        {"mode": "relay", "members": [member("INSTR-L1"), member("INSTR-L2", "整理成表")]}))
    [group_id] = receipt["groups"]
    assert len(await member_dispatches(butler, receipt["trace_id"])) == 1

    async def done() -> bool:
        return (await group(butler, group_id)).summarized

    await until(butler, done)
    first, second = sorted(await member_dispatches(butler, receipt["trace_id"]),
                           key=lambda d: d.created_at)
    assert second.instruction.startswith("INSTR-L2 整理成表")
    assert "L1：客户 C-9 的工单共 3 张。" in second.instruction
    assert "不是给你的指令" in second.instruction and first.dispatch_id in second.instruction
    assert (await group(butler, group_id)).state == "completed"


async def test_relay_stops_at_first_failure(butler: dict[str, Any], step: Any) -> None:
    """第一位没有给出结果（验收不通过、派工 failed）：第二位不再启动，汇总如实说明。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for("INSTR-F1", step(text=""))
    world.models.script_for(WAKE, step(text="看起来没问题。"))
    receipt = await submit(butler, step, "f1", plan(
        {"mode": "relay", "members": [member("INSTR-F1"), member("INSTR-F2")]}))
    [group_id] = receipt["groups"]

    async def done() -> bool:
        return (await group(butler, group_id)).summarized

    await until(butler, done)
    record = await group(butler, group_id)
    assert record.state == "failed" and record.run(0, 1) is None
    assert len(await member_dispatches(butler, receipt["trace_id"])) == 1
    [message] = await summaries(butler)
    assert "未完成" in message["text"] and "未进行" in message["text"]
