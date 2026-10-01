"""空闲回收：会话空闲后释放引擎、运行时空闲后回收；并发承载量靠这个。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.runtime import RuntimeDefaults

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


class Clock:
    """可拨动的时钟。"""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """往前拨。"""
        self.now += seconds


@pytest.fixture
async def timed(world: Any) -> Any:
    """同一份存储上的另一个实例，用可拨动的时钟，空闲阈值很短。

    会话从建到释放再续接都在这一个实例上，不跨实例接手，所以它有自己的数据目录，
    不与 world 共用（local 状态面下会话主存就在它自己的数据目录里）。
    """
    clock = Clock()
    data_dir = world.data_dir.parent / "data-timed"
    data_dir.mkdir()
    platform = world.sibling(
        "inst-timed",
        data_dir=data_dir,
        defaults=RuntimeDefaults(session_idle_seconds=60, runtime_idle_seconds=120),
        now=clock,
    )
    try:
        yield platform, clock
    finally:
        await platform.close()


def live_count(platform: Platform) -> int:
    """本实例持有的会话数。"""
    return len(platform.sessions._live)  # noqa: SLF001


def runtime_count(platform: Platform) -> int:
    """已组装的运行时数。"""
    return len(platform.runtimes._runtimes)  # noqa: SLF001


async def test_idle_session_is_released_and_resumes(
    world: Any, step: Any, read_turn: Any, timed: Any
) -> None:
    """空闲超过阈值：引擎释放、租约放弃，会话仍活动；再对话时从存储续接，上下文还在。"""
    platform, clock = timed
    await world.publish()
    session = await platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="订单号 A-1024"), step(text="还是 A-1024"))
    await read_turn(await platform.sessions.send_message(world.member, session.session_id, "查"))
    assert live_count(platform) == 1

    clock.advance(30)
    await platform.sessions.maintain()
    assert live_count(platform) == 1

    clock.advance(31)
    assert await platform.sessions.release_idle() == [session.session_id]
    assert live_count(platform) == 0
    stored = await platform.sessions.get(world.member, session.session_id)
    assert (stored.status, stored.owner) == ("active", None)
    assert (
        session.session_id not in next(iter(platform.runtimes._runtimes.values())).pool.introspect()
    )  # noqa: SLF001

    events = await read_turn(
        await platform.sessions.send_message(world.member, session.session_id, "刚才的订单号")
    )
    assert events[-1].msg.kind == "turn_completed"
    assert "订单号 A-1024" in world.models.last_prompt()
    assert live_count(platform) == 1


async def test_busy_session_is_not_released(
    world: Any, step: Any, read_turn: Any, timed: Any
) -> None:
    """有进行中的对话时，无论多久都不释放。"""
    platform, clock = timed
    await world.publish()
    session = await platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="慢"))
    turn = await platform.sessions.send_message(world.member, session.session_id, "问")
    clock.advance(10_000)
    # 事件还没被读走，但一轮的结束由枢纽记录，与消费方无关；这里先确认结束前的状态
    live = platform.sessions._live[("acme", session.session_id)]  # noqa: SLF001
    if live.in_flight:
        assert await platform.sessions.release_idle() == []
    await read_turn(turn)
    for _ in range(50):
        if live.idle:
            break
        await asyncio.sleep(0.01)
    assert live.idle
    # 结束那一刻算最近活动，之后还要再空闲够阈值才释放
    assert await platform.sessions.release_idle() == []
    clock.advance(61)
    assert await platform.sessions.release_idle() == [session.session_id]


async def test_suspended_session_is_released_and_resumable(
    world: Any, step: Any, read_turn: Any, timed: Any
) -> None:
    """等审批的会话也可以释放引擎；审批结果送回时从存储续接并续跑。"""
    platform, clock = timed
    await world.publish()
    session = await platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]), step(text="更新好了")
    )
    events = await read_turn(
        await platform.sessions.send_message(world.member, session.session_id, "更新")
    )
    request_id = events[-1].msg.data["pending"][0]["request_id"]
    clock.advance(61)
    assert await platform.sessions.release_idle() == [session.session_id]
    assert await platform.sessions.pending(world.member, session.session_id) != []

    resumed = await read_turn(
        await platform.sessions.resume(
            world.member, session.session_id, {request_id: {"granted": True}}
        )
    )
    assert resumed[-1].msg.kind == "turn_completed"
    assert world.calls() == [{"name": "update_ticket", "arguments": {"id": "T-1"}}]


async def test_idle_runtime_is_evicted_and_rebuilt(
    world: Any, step: Any, read_turn: Any, timed: Any
) -> None:
    """没有会话在用的运行时空闲超过阈值后回收（连接器进程随之关闭）；再用时重新组装。"""
    platform, clock = timed
    await world.publish()
    session = await platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="a"), step(text="b"))
    await read_turn(await platform.sessions.send_message(world.member, session.session_id, "1"))
    assert runtime_count(platform) == 1
    first_runtime = next(iter(platform.runtimes._runtimes.values()))  # noqa: SLF001

    clock.advance(121)
    assert await platform.runtimes.evict_idle() == []  # 会话还占着引擎
    await platform.sessions.maintain()
    assert runtime_count(platform) == 0

    await read_turn(await platform.sessions.send_message(world.member, session.session_id, "2"))
    assert runtime_count(platform) == 1
    assert next(iter(platform.runtimes._runtimes.values())) is not first_runtime  # noqa: SLF001


async def test_many_sessions_do_not_accumulate_engines(
    world: Any, step: Any, read_turn: Any, timed: Any
) -> None:
    """大量会话轮流对话：维护之后常驻的引擎只剩最近活跃的那些。"""
    platform, clock = timed
    await world.publish()
    sessions = [
        await platform.sessions.create(world.member, package="support-desk") for _ in range(30)
    ]
    world.models.script(*(step(text=f"r{i}") for i in range(30)))
    for index, item in enumerate(sessions):
        await read_turn(
            await platform.sessions.send_message(world.member, item.session_id, f"q{index}")
        )
        clock.advance(5)
    assert live_count(platform) == 30
    await platform.sessions.maintain()
    # 5 秒一个，60 秒阈值：只有最后约 12 个还在
    assert live_count(platform) <= 12
    assert await platform.store.count_active_sessions("acme") == 30
