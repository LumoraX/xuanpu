"""多实例（M0 验收）：状态面用 Postgres 时，各实例各用各的数据目录，照样能接手彼此的会话。

几个平台实例共用一个控制面（Postgres）、一个状态面（同一 schema 下的 Postgres）与一个对象
存储，数据目录各自独立。会话主存在库里，所以接手不需要共享数据目录。local 状态面下的行为
由其余用例（``world`` 夹具）覆盖。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
import taifeng
from taifeng_stores import SqlNamespace
from taifeng_stores.postgres import PostgresMessageStore, PostgresThreadDirectory
from xuanpu_server.bootstrap import PostgresStateBackends, tenant_table_prefix
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.errors import SessionBusyError
from xuanpu_server.runtime import LocalSandboxProvider, StateBackends

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal


@dataclass
class Instance:
    """一个平台实例，外加测试里要用的把手。"""

    platform: Platform
    member: Principal
    data_dir: Path
    publish: Callable[[], Awaitable[None]]


@dataclass
class Cluster:
    """共用控制面、状态面与对象存储的一组平台实例；第一个实例负责开通租户。"""

    world: Any
    pool: asyncpg.Pool
    schema: str
    states: PostgresStateBackends
    started: list[Platform] = field(default_factory=list)

    @property
    def first(self) -> Instance:
        """开通租户的那个实例（数据目录 ``data-a``）。"""
        return Instance(
            platform=self.world.platform,
            member=self.world.member,
            data_dir=self.world.data_dir,
            publish=self.world.publish,
        )

    async def spawn(self, instance_id: str, data_dir_name: str, **kwargs: Any) -> Instance:
        """在 ``data_dir_name`` 这个数据目录上起一个实例（目录已在就沿用，用来模拟重启）。"""
        data_dir = (self.world.data_dir.parent / data_dir_name).resolve()
        data_dir.mkdir(exist_ok=True)
        platform = self.world.sibling(instance_id, data_dir=data_dir, **kwargs)
        await platform.start()
        self.started.append(platform)

        async def publish() -> None:
            """经这个实例发布样例业务包并设为生效版本。"""
            await platform.packages.publish(self.world.admin, self.world.archive(), activate=True)

        return Instance(
            platform=platform, member=self.world.member, data_dir=data_dir, publish=publish
        )

    async def stored_messages(self, thread_id: str) -> list[tuple[str, str | None]]:
        """库里这个线程的消息，按回放顺序：``(种类, 文本)``。"""
        table = f'"{self.schema}"."{tenant_table_prefix("acme")}message"'
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT kind, payload->>'text' AS text FROM {table} "
                "WHERE thread_id = $1 ORDER BY position, seq",
                thread_id,
            )
        return [(row["kind"], row["text"]) for row in rows]

    async def close(self) -> None:
        """后起的先关，最后关开通租户的那个。关过的平台再关一次无害。"""
        for platform in reversed(self.started):
            await platform.close()
        await self.world.platform.close()


@pytest.fixture
async def cluster(postgres_dsn: str, world_opener: Any) -> AsyncIterator[Cluster]:
    """一个独占 schema 的 Postgres（控制面与状态面都在里面）上的平台集群，先只有实例 A。"""
    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=10)
    schema = "t_" + secrets.token_hex(6)
    try:
        namespace = SqlNamespace(schema=schema, prefix="xp_")
        await apply_migrations(pool, namespace)
        states = PostgresStateBackends(pool, schema=schema)
        world = await world_opener(
            store=PostgresControlPlane(pool, namespace=namespace),
            sandboxes=LocalSandboxProvider(),
            states=states,
            instance_id="inst-a",
            data_dir_name="data-a",
        )
        group = Cluster(world=world, pool=pool, schema=schema, states=states)
        try:
            yield group
        finally:
            await group.close()
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await pool.close()


@pytest.fixture
async def two_instances(cluster: Cluster) -> tuple[Instance, Instance, Any]:
    """实例 A 与 B：数据目录不同、实例标识不同，其余全部共用；外加共用的脚本化模型。"""
    return cluster.first, await cluster.spawn("inst-b", "data-b"), cluster.world.models


def dialogue(request: Any) -> list[tuple[str, str]]:
    """一次采样请求里的对话部分：``(角色, 文本)``，只取用户与助手的纯文本消息。"""
    return [
        (message.role, message.content)
        for message in request.messages or []
        if message.role in ("user", "assistant") and isinstance(message.content, str)
    ]


async def test_second_instance_takes_over_without_a_shared_data_dir(
    two_instances: Any, step: Any, read_turn: Any
) -> None:
    """A 跑一轮后放手；B 用另一个数据目录接手同一会话，模型看得到上一轮。"""
    a, b, models = two_instances
    await a.publish()
    session = await a.platform.sessions.create(a.member, package="support-desk")
    models.script(step(text="第一轮"))
    await read_turn(await a.platform.sessions.send_message(a.member, session.session_id, "记住 42"))
    await a.platform.sessions.shutdown()  # 放手：释放引擎与租约

    models.script(step(text="第二轮"))
    events = await read_turn(
        await b.platform.sessions.send_message(b.member, session.session_id, "刚才是几")
    )
    assert events[-1].msg.kind == "turn_completed"
    assert "记住 42" in models.last_prompt()
    assert not (b.data_dir / "threads").exists()
    # 主存不落本地：A 的数据目录里也没有会话文件
    assert not (a.data_dir / "threads").exists()
    taken = await b.platform.sessions.get(b.member, session.session_id)
    assert taken.owner == "inst-b"


async def test_session_goes_back_and_forth_with_history_in_order(
    cluster: Cluster, two_instances: Any, step: Any, read_turn: Any
) -> None:
    """A → B → A（重启后的 A）各跑一轮：每次接手都看得到之前全部的对话，库里三轮完整有序。"""
    a, b, models = two_instances
    await b.publish()
    session = await a.platform.sessions.create(a.member, package="support-desk")
    models.script(step(text="第一轮"), step(text="第二轮"), step(text="第三轮"))
    await read_turn(await a.platform.sessions.send_message(a.member, session.session_id, "一问"))
    await a.platform.close()  # A 整个退出

    await read_turn(await b.platform.sessions.send_message(b.member, session.session_id, "二问"))
    await b.platform.sessions.shutdown()

    # A 在原来的数据目录上重启（同一个实例标识）
    restarted = await cluster.spawn("inst-a", "data-a")
    events = await read_turn(
        await restarted.platform.sessions.send_message(restarted.member, session.session_id, "三问")
    )
    assert events[-1].msg.kind == "turn_completed"

    history = [
        ("user", "一问"),
        ("assistant", "第一轮"),
        ("user", "二问"),
        ("assistant", "第二轮"),
        ("user", "三问"),
    ]
    assert dialogue(models.requests[-1]) == history
    record = await restarted.platform.sessions.get(restarted.member, session.session_id)
    assert record.thread_id is not None
    kinds = {"user": "user_message", "assistant": "assistant_message"}
    assert await cluster.stored_messages(record.thread_id) == [
        *((kinds[role], text) for role, text in history),
        ("assistant_message", "第三轮"),
    ]
    for instance in (a, b, restarted):
        assert not (instance.data_dir / "threads").exists()


async def test_crashed_holder_is_taken_over_after_lease_expiry(
    cluster: Cluster, step: Any, read_turn: Any
) -> None:
    """持有者没放手就没了（不调 shutdown）：租约到期前别的实例接不了，到期后能接手并续上上下文；
    原持有者之后续期失败，立即放手。"""
    clock = {"now": 1_000.0}

    def now() -> float:
        """两个实例共用的可拨动时钟。"""
        return clock["now"]

    crashed = await cluster.spawn("inst-crashed", "data-c", now=now, lease_seconds=60)
    survivor = await cluster.spawn("inst-survivor", "data-s", now=now, lease_seconds=60)
    models = cluster.world.models
    await crashed.publish()
    session = await crashed.platform.sessions.create(crashed.member, package="support-desk")
    models.script(step(text="第一轮"), step(text="第二轮"))
    await read_turn(
        await crashed.platform.sessions.send_message(crashed.member, session.session_id, "记住 42")
    )

    clock["now"] = 1_059.0
    with pytest.raises(SessionBusyError):
        await survivor.platform.sessions.send_message(survivor.member, session.session_id, "抢")
    clock["now"] = 1_061.0
    events = await read_turn(
        await survivor.platform.sessions.send_message(survivor.member, session.session_id, "几")
    )
    assert events[-1].msg.kind == "turn_completed"
    assert dialogue(models.requests[-1])[:2] == [("user", "记住 42"), ("assistant", "第一轮")]

    # 原持有者恢复过来：续期失败，放掉引擎，不再碰这个会话
    await crashed.platform.sessions.maintain()
    assert crashed.platform.sessions._live == {}  # noqa: SLF001
    taken = await survivor.platform.sessions.get(survivor.member, session.session_id)
    assert taken.owner == "inst-survivor"
    assert taken.thread_id is not None
    assert [kind for kind, _ in await cluster.stored_messages(taken.thread_id)] == [
        "user_message",
        "assistant_message",
        "user_message",
        "assistant_message",
    ]


async def test_postgres_state_uses_the_store_as_primary_without_a_mirror(
    cluster: Cluster, step: Any, read_turn: Any
) -> None:
    """postgres 状态面给的是会话主存与配套的线程目录，不挂消息镜像；一轮的消息同步落库。"""
    backends = await cluster.states.for_tenant("acme")
    # 把住配置的是这一条：主存与镜像写同一张表，双挂会让镜像用未经回放校准的位置抢写、
    # 写入量翻倍（表上有唯一约束，重复的行会被丢掉，所以下面数行数看不出双挂）
    assert backends.index_hook is None
    assert isinstance(backends.thread_directory, PostgresThreadDirectory)
    assert backends.message_store is not None
    assert isinstance(backends.message_store(), PostgresMessageStore)
    # 每次调用给一个新的存储实例：每个运行时各用各的
    assert backends.message_store() is not backends.message_store()

    a, models = cluster.first, cluster.world.models
    await a.publish()
    session = await a.platform.sessions.create(a.member, package="support-desk")
    models.script(step(text="第一轮"))
    await read_turn(await a.platform.sessions.send_message(a.member, session.session_id, "记住 42"))
    record = await a.platform.sessions.get(a.member, session.session_id)
    assert record.thread_id is not None
    # 一轮结束时消息已经在库里（主存同步写，不像镜像那样事后补写）
    assert await cluster.stored_messages(record.thread_id) == [
        ("user_message", "记住 42"),
        ("assistant_message", "第一轮"),
    ]


def test_message_store_and_mirror_cannot_be_combined() -> None:
    """同时给会话主存与消息镜像会双写同一张表：构造时就报错。"""
    with pytest.raises(ValueError, match="index_hook"):
        StateBackends(
            message_store=lambda: pytest.fail("构造时不该创建存储"),
            index_hook=taifeng.NoopIndexHook(),
        )
