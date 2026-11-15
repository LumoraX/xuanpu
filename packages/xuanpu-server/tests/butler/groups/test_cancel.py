"""取消编组：未启动的成员不再启动，进行中的请求停止，按真实终态收敛并汇总一次。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from xuanpu_server.errors import NotFoundError
from xuanpu_server.tracing.models import TraceOwner

from ..conftest import enable
from .helpers import ALICE, WAKE, group, member, member_dispatches, plan, submit, summaries, until


async def test_cancel_stops_running_and_skips_pending(butler: dict[str, Any], step: Any) -> None:
    """接力第一位卡住时取消：第一位被停下（真实终态），第二位从未启动，编组 cancelled。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    hold = asyncio.Event()
    world.models.script_for("INSTR-C1", step(text="还在查", wait=hold))
    world.models.script_for(WAKE, step(text="可以。"))
    receipt = await submit(butler, step, "c1", plan(
        {"mode": "relay", "members": [member("INSTR-C1"), member("INSTR-C2")]}))
    [group_id] = receipt["groups"]
    record = await world.platform.butler.groups.cancel(ALICE, group_id)
    assert record.cancel_requested and record.state == "running"

    async def done() -> bool:
        return (await group(butler, group_id)).summarized

    await until(butler, done)
    hold.set()
    record = await group(butler, group_id)
    assert record.state == "cancelled"
    assert (record.run(0, 1).outcome, record.run(0, 1).end_reason) == ("cancelled",
                                                                        "group_cancelled")
    [first] = await member_dispatches(butler, receipt["trace_id"])
    assert first.state in ("cancelled", "failed", "completed") and not first.active
    [message] = await summaries(butler)
    assert "已停止" in message["text"]


async def test_other_employee_cannot_see_or_cancel(butler: dict[str, Any], step: Any) -> None:
    """他人读取或取消编组一律 404。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    hold = asyncio.Event()
    world.models.script_for("INSTR-O1", step(text="处理中", wait=hold))
    receipt = await submit(butler, step, "o1", plan(
        {"mode": "parallel", "members": [member("INSTR-O1")]}))
    [group_id] = receipt["groups"]
    bob = TraceOwner(tenant_id="acme", user_id="usr_bob")
    with pytest.raises(NotFoundError):
        await world.platform.butler.groups.get(bob, group_id)
    with pytest.raises(NotFoundError):
        await world.platform.butler.groups.cancel(bob, group_id)
    hold.set()
    await until(butler, lambda: _summarized(butler, group_id))


async def _summarized(butler: dict[str, Any], group_id: str) -> bool:
    """编组是否已汇总。"""
    return bool((await group(butler, group_id)).summarized)
