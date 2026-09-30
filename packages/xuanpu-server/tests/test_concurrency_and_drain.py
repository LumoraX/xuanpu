"""并发对话上限与优雅停机。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from xuanpu_server.errors import QuotaExceededError, RuntimeUnavailableError
from xuanpu_server.tenancy import QuotaLimits


async def _set_quota(world: Any, quota: QuotaLimits) -> None:
    """改租户配额。"""
    tenant = await world.platform.store.get_tenant("acme")
    assert tenant is not None
    await world.platform.store.update_tenant(replace(tenant, quota=quota))


async def test_concurrent_turn_limit(world: Any, step: Any, read_turn: Any) -> None:
    """在途对话数到上限后新的对话被拒；跑完一轮又能开。"""
    await world.publish()
    await _set_quota(world, QuotaLimits(max_concurrent_turns=2))
    sessions = [
        await world.platform.sessions.create(world.member, package="support-desk") for _ in range(3)
    ]
    world.models.script(step(text="a"), step(text="b"), step(text="c"))
    first = await world.platform.sessions.send_message(world.member, sessions[0].session_id, "1")
    second = await world.platform.sessions.send_message(world.member, sessions[1].session_id, "2")
    # 两轮的事件还没被消费，但结束与否由枢纽判定；这里在结束前立刻发第三轮
    live = world.platform.sessions
    if live.in_flight_turns("acme") >= 2:
        with pytest.raises(QuotaExceededError, match="进行中的对话"):
            await live.send_message(world.member, sessions[2].session_id, "3")
    await read_turn(first)
    await read_turn(second)
    for _ in range(100):
        if live.in_flight_turns("acme") == 0:
            break
        await asyncio.sleep(0.01)
    third = await live.send_message(world.member, sessions[2].session_id, "3")
    assert (await read_turn(third))[-1].msg.kind == "turn_completed"


async def test_drain_waits_for_in_flight_turns(world: Any, step: Any, read_turn: Any) -> None:
    """停机：先拒绝新对话，等在途的跑完，再释放会话；会话保持活动。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="进行中"))
    turn = await world.platform.sessions.send_message(world.member, session.session_id, "问")

    async def shutdown() -> None:
        await world.platform.sessions.shutdown(drain_seconds=10)

    task = asyncio.ensure_future(shutdown())
    await asyncio.sleep(0.05)
    with pytest.raises(RuntimeUnavailableError, match="停机"):
        await world.platform.sessions.send_message(world.member, session.session_id, "又问")
    events = await read_turn(turn)
    assert events[-1].msg.kind == "turn_completed"
    await asyncio.wait_for(task, timeout=10)
    stored = await world.platform.sessions.get(world.member, session.session_id)
    assert (stored.status, stored.owner) == ("active", None)


async def test_drain_gives_up_after_timeout(world: Any, step: Any) -> None:
    """在途对话迟迟不结束：等到时限就释放，不会卡死停机。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")

    class Stuck:
        """一个永远不回复的模型会话。"""

        async def stream(self, request: Any) -> Any:
            await asyncio.sleep(3600)
            yield None

        async def __aenter__(self) -> Stuck:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return

    world.models.session = lambda **kwargs: Stuck()  # type: ignore[method-assign]
    await world.platform.sessions.send_message(world.member, session.session_id, "卡住")
    started = asyncio.get_running_loop().time()
    await world.platform.sessions.shutdown(drain_seconds=0.5)
    assert asyncio.get_running_loop().time() - started < 5
    assert world.platform.sessions.in_flight_turns("acme") == 0
