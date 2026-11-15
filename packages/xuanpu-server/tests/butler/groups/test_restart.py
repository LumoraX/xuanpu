"""重启续跑：进程在「派出成员后、保存前」或「写汇总后、标记前」被终止，重启后
不重复派工、不重复汇总。用全新的执行器实例模拟重启（执行器不持有进程内状态）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from xuanpu_server.butler.groups.runner import GroupRunner

from ..conftest import enable
from .helpers import ALICE, WAKE, group, member, member_dispatches, plan, submit, summaries, until


class Crash(Exception):
    """模拟进程在此处被终止。"""


def _crash_once(store: Any, monkeypatch: Any, when: Any) -> dict[str, bool]:
    """让下一次满足条件的保存失败一次（之前的写入都已落库）。"""
    original = store.save
    state = {"armed": True}

    async def save(owner: Any, record: Any, *, expected_version: int) -> Any:
        """满足条件时抛出一次。"""
        if state["armed"] and when(record):
            state["armed"] = False
            raise Crash
        return await original(owner, record, expected_version=expected_version)

    monkeypatch.setattr(store, "save", save)
    return state


def _restarted(world: Any) -> GroupRunner:
    """一个全新的执行器（模拟重启后的进程）。"""
    butler = world.platform.butler
    return GroupRunner(world.platform, butler.dispatcher, butler.group_store, now=time.time)


async def test_crash_between_launch_and_save_does_not_relaunch(
    butler: dict[str, Any], step: Any, monkeypatch: Any,
) -> None:
    """接力第二位派出后、编组保存前终止：重启推进回放原派工，第二位只有一次。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    hold = asyncio.Event()
    world.models.script_for("INSTR-S1", step(text="S1 完成。"))
    world.models.script_for("INSTR-S2", step(text="S2 完成。", wait=hold))
    world.models.script_for(WAKE, step(text="可以。"), step(text="可以。"))
    receipt = await submit(butler, step, "s1", plan(
        {"mode": "relay", "members": [member("INSTR-S1"), member("INSTR-S2")]}))
    [group_id] = receipt["groups"]
    crash = _crash_once(world.platform.butler.group_store, monkeypatch,
                        lambda r: r.run(0, 1) is not None and r.run(0, 1).outcome is None)

    async def crashed() -> bool:
        return not crash["armed"]

    # 唤醒循环里的推进在保存处被「终止」；它只记日志，不会自己补救
    await until(butler, crashed)
    assert len(await member_dispatches(butler, receipt["trace_id"])) == 2
    assert (await group(butler, group_id)).run(0, 1) is None
    await _restarted(world).advance(ALICE, group_id)
    assert len(await member_dispatches(butler, receipt["trace_id"])) == 2
    assert (await group(butler, group_id)).run(0, 1) is not None
    hold.set()
    await until(butler, lambda: _summarized(butler, group_id))
    assert len(await member_dispatches(butler, receipt["trace_id"])) == 2
    assert len(await summaries(butler)) == 1


async def test_crash_after_summary_before_marker_does_not_resummarize(
    butler: dict[str, Any], step: Any, monkeypatch: Any,
) -> None:
    """成员都完成、汇总消息已写、标记前终止：重启后只补标记，汇总仍是一条。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for("INSTR-T1", step(text="T1 完成。"))
    world.models.script_for(WAKE, step(text="可以。"))
    receipt = await submit(butler, step, "t1", plan(
        {"mode": "parallel", "members": [member("INSTR-T1")]}))
    [group_id] = receipt["groups"]
    crash = _crash_once(world.platform.butler.group_store, monkeypatch, lambda r: r.summarized)

    async def crashed() -> bool:
        return not crash["armed"]

    await until(butler, crashed)
    assert len(await summaries(butler)) == 1
    assert not (await group(butler, group_id)).summarized
    record = await _restarted(world).advance(ALICE, group_id)
    assert record.summarized
    assert len(await summaries(butler)) == 1
    assert len(await member_dispatches(butler, receipt["trace_id"])) == 1


async def _summarized(butler: dict[str, Any], group_id: str) -> bool:
    """编组是否已汇总。"""
    return bool((await group(butler, group_id)).summarized)
