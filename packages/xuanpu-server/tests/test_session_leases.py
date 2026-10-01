"""会话持有权：单写者、放手后接手、崩溃后租约到期接手，以及准备引擎期间持有权的变化。

这里的几个实例共用 ``world`` 的数据目录：world 用 local 状态面，会话主存在数据目录里，
跨实例接手只能共用它——这违反「数据目录归一个进程独占」的前提，但这里测的是会话的
持有与接手，与该前提无关。postgres 状态面下各实例各用各的数据目录，见
test_multi_instance.py。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import ConflictError, LeaseInterruptedError, SessionBusyError

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


class Clock:
    """可拨动的时钟，几个实例共用。"""

    def __init__(self) -> None:
        """从 1000 秒开始。"""
        self.now = 1_000.0

    def __call__(self) -> float:
        """当前时间。"""
        return self.now


def _another_instance(world: Any, instance_id: str, **kwargs: Any) -> Platform:
    """同一份存储、对象存储与数据目录上的另一个平台实例（共用数据目录的理由见模块说明）。"""
    return world.sibling(instance_id, data_dir=world.data_dir, **kwargs)


def _slow_instance(world: Any, clock: Clock) -> Platform:
    """用可拨动时钟、租约 60 秒的实例，用来模拟准备引擎很慢。"""
    return _another_instance(world, "inst-slow", now=clock, lease_seconds=60)


def _only_runtime(platform: Platform) -> Any:
    """实例上唯一一个已组装的运行时。"""
    runtimes = list(platform.runtimes._runtimes.values())  # noqa: SLF001
    assert len(runtimes) == 1
    return runtimes[0]


async def test_session_has_a_single_holder(world: Any, step: Any, read_turn: Any) -> None:
    """一个实例持有会话时，另一个实例不能同时对它写；提示里带上持有者。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="hi"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    other = _another_instance(world, "inst-other")
    try:
        with pytest.raises(SessionBusyError) as caught:
            await other.sessions.send_message(world.member, session.session_id, "抢")
        assert caught.value.owner == "inst-test"
    finally:
        await other.close()


async def test_another_instance_takes_over_after_release(
    world: Any, step: Any, read_turn: Any
) -> None:
    """实例退出后，另一个实例从共用的会话存储里续接：上下文还在，审计接得上。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="订单号是 A-1024"), step(text="是 A-1024"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查订单")
    )
    thread_id = (await world.platform.sessions.get(world.member, session.session_id)).thread_id
    await world.platform.close()

    released = await world.platform.store.get_session("acme", session.session_id)
    assert released is not None
    assert (released.status, released.owner) == ("active", None)

    other = _another_instance(world, "inst-other")
    try:
        events = await read_turn(
            await other.sessions.send_message(world.member, session.session_id, "刚才的订单号？")
        )
        assert events[-1].msg.kind == "turn_completed"
        assert "订单号是 A-1024" in world.models.last_prompt()
        taken = await other.sessions.get(world.member, session.session_id)
        assert (taken.owner, taken.thread_id) == ("inst-other", thread_id)
    finally:
        await other.close()

    completed = await world.platform.store.query_audit(
        "acme", AuditQuery(kind="turn_completed", session_id=session.session_id)
    )
    assert len(completed) == 2
    # 两个实例各是一条事件流，序号各自从头计，互不去重
    assert len({record.stream for record in completed}) == 2


async def test_expired_lease_can_be_taken_over(world: Any, step: Any) -> None:
    """持有者没来得及放手就没了（崩溃）：租约到期后别的实例可以接手，到期前不行。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    clock = Clock()
    crashed = _another_instance(world, "inst-crashed", now=clock, lease_seconds=60)
    world.models.script(step(text="一"), step(text="二"))
    await crashed.sessions.send_message(world.member, session.session_id, "1")

    survivor = _another_instance(world, "inst-survivor", now=clock, lease_seconds=60)
    try:
        clock.now = 1_059.0
        with pytest.raises(SessionBusyError):
            await survivor.sessions.send_message(world.member, session.session_id, "2")
        clock.now = 1_061.0
        await survivor.sessions.send_message(world.member, session.session_id, "2")
        taken = await survivor.sessions.get(world.member, session.session_id)
        assert taken.owner == "inst-survivor"
    finally:
        await survivor.close()
        await crashed.close()


async def test_lapsed_lease_is_dropped_on_renewal(world: Any, step: Any, read_turn: Any) -> None:
    """租约到期之后才轮到续期：即使没人接手，也按丢失放手（中间可能有人写过），下次重新续接。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    clock = Clock()
    stalled = _another_instance(world, "inst-stalled", now=clock, lease_seconds=60)
    try:
        world.models.script(step(text="第一轮"), step(text="第二轮"))
        await read_turn(await stalled.sessions.send_message(world.member, session.session_id, "1"))
        clock.now = 1_061.0
        await stalled.sessions.maintain()
        assert stalled.sessions._live == {}  # noqa: SLF001
        record = await world.platform.store.get_session("acme", session.session_id)
        assert record is not None
        assert record.owner is None

        events = await read_turn(
            await stalled.sessions.send_message(world.member, session.session_id, "2")
        )
        assert events[-1].msg.kind == "turn_completed"
        assert "第一轮" in world.models.last_prompt()
    finally:
        await stalled.close()


async def _lease_taken_by_another(world: Any, session_id: str, clock: Clock) -> None:
    """时钟拨过租约期限，另一个实例趁机拿走会话的租约。"""
    clock.now = 1_061.0
    await world.platform.store.acquire_lease(
        "acme", session_id, owner="inst-other", now=clock.now, ttl_seconds=60
    )


async def _assert_not_held(world: Any, holder: Platform, session_id: str) -> None:
    """``holder`` 没有持有这个会话：不在本实例的会话里，没有采样，线程也没有记到会话上。"""
    assert holder.sessions._live == {}  # noqa: SLF001
    assert world.models.requests == []
    record = await world.platform.store.get_session("acme", session_id)
    assert record is not None
    assert (record.owner, record.thread_id) == ("inst-other", None)
    sent = await world.platform.store.query_audit(
        "acme", AuditQuery(kind="message_sent", session_id=session_id)
    )
    assert sent == []


async def test_lease_lost_while_assembling_is_not_used(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """组装运行时比租约还久、期间别的实例拿走了租约：这次请求失败，不续接、不提交任何一轮。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    clock = Clock()
    slow = _slow_instance(world, clock)
    create_client = world.models.create

    async def assemble_slowly(tenant_id: str, tier: str) -> Any:
        """组装运行时要取模型客户端：在这里耗掉整个租约期限。"""
        await _lease_taken_by_another(world, session.session_id, clock)
        return await create_client(tenant_id, tier)

    monkeypatch.setattr(world.models, "create", assemble_slowly)
    try:
        with pytest.raises(SessionBusyError) as caught:
            await slow.sessions.send_message(world.member, session.session_id, "你好")
        assert caught.value.owner == "inst-other"
        await _assert_not_held(world, slow, session.session_id)
        runtime = _only_runtime(slow)
        assert runtime.pool.introspect() == {}  # 没有为它建引擎
        # 续接之前就停下了：没有在别人的租约下建线程
        assert await runtime.pool.store.list_threads(limit=50) == []
    finally:
        await slow.close()


async def test_lease_lost_while_starting_the_engine_is_not_used(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """建引擎（续接历史）期间租约被拿走：刚建的引擎被关掉，不放进本实例持有的会话里。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    clock = Clock()
    slow = _slow_instance(world, clock)
    try:
        resolved = await slow.packages.resolve("acme", "support-desk", None)
        runtime = await slow.runtimes.get(resolved)
        get_or_create = runtime.pool.get_or_create

        async def start_slowly(**kwargs: Any) -> Any:
            """引擎建好之后、放进本实例之前，租约被别人拿走。"""
            engine = await get_or_create(**kwargs)
            await _lease_taken_by_another(world, session.session_id, clock)
            return engine

        monkeypatch.setattr(runtime.pool, "get_or_create", start_slowly)
        with pytest.raises(SessionBusyError) as caught:
            await slow.sessions.send_message(world.member, session.session_id, "你好")
        assert caught.value.owner == "inst-other"
        await _assert_not_held(world, slow, session.session_id)
        assert runtime.pool.introspect() == {}  # 建好的引擎已经关掉
    finally:
        await slow.close()


async def test_takeover_during_assembly_does_not_rebind_the_thread(
    world: Any, step: Any, read_turn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """慢实例组装期间租约过期，别的实例接手、建线程、跑一轮又放手；慢实例随后又能续上租约。

    中间断过，慢实例这次请求作废（可重试），不拿旧记录另建线程覆盖别人的；重试时从存储
    续接，看得到别人那一轮。
    """
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    clock = Clock()
    slow = _slow_instance(world, clock)
    fast = _another_instance(world, "inst-fast", now=clock, lease_seconds=60)
    create_client = world.models.create
    taken_over: dict[str, str | None] = {}

    async def assemble_slowly(tenant_id: str, tier: str) -> Any:
        """慢实例第一次组装时：租约过期，快实例接手、跑一轮、放手。"""
        # 快实例自己组装时也会走到这里：先占位，免得重入
        if "thread_id" not in taken_over:
            taken_over["thread_id"] = None
            clock.now = 1_061.0
            world.models.script(step(text="快实例的回答"))
            turn = await fast.sessions.send_message(world.member, session.session_id, "记住 42")
            await read_turn(turn)
            await fast.sessions.shutdown()
            record = await world.platform.store.get_session("acme", session.session_id)
            taken_over["thread_id"] = record.thread_id
        return await create_client(tenant_id, tier)

    monkeypatch.setattr(world.models, "create", assemble_slowly)
    try:
        with pytest.raises(LeaseInterruptedError):
            await slow.sessions.send_message(world.member, session.session_id, "刚才是几")
        assert slow.sessions._live == {}  # noqa: SLF001
        record = await world.platform.store.get_session("acme", session.session_id)
        assert record is not None
        assert taken_over["thread_id"] is not None
        assert (record.thread_id, record.owner) == (taken_over["thread_id"], None)
        assert len(world.models.requests) == 1  # 只有快实例那一轮

        world.models.script(step(text="慢实例的回答"))
        events = await read_turn(
            await slow.sessions.send_message(world.member, session.session_id, "刚才是几")
        )
        assert events[-1].msg.kind == "turn_completed"
        assert "记住 42" in world.models.last_prompt()
        record = await world.platform.store.get_session("acme", session.session_id)
        assert (record.thread_id, record.owner) == (taken_over["thread_id"], "inst-slow")
    finally:
        await slow.close()
        await fast.close()


async def test_session_closed_during_assembly_is_not_opened(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """慢实例组装期间会话被别的实例关闭：按会话已关闭报错，不建线程、不开一轮。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    slow = _slow_instance(world, Clock())
    create_client = world.models.create

    async def assemble_slowly(tenant_id: str, tier: str) -> Any:
        """组装期间，另一个实例关闭了这个会话（关闭不要求持有）。"""
        await world.platform.sessions.close(world.member, session.session_id)
        return await create_client(tenant_id, tier)

    monkeypatch.setattr(world.models, "create", assemble_slowly)
    try:
        with pytest.raises(ConflictError, match="已关闭"):
            await slow.sessions.send_message(world.member, session.session_id, "你好")
        assert slow.sessions._live == {}  # noqa: SLF001
        assert world.models.requests == []
        runtime = _only_runtime(slow)
        assert runtime.pool.introspect() == {}
        assert await runtime.pool.store.list_threads(limit=50) == []
        record = await world.platform.store.get_session("acme", session.session_id)
        assert record is not None
        assert (record.status, record.owner, record.thread_id) == ("closed", None, None)
    finally:
        await slow.close()
