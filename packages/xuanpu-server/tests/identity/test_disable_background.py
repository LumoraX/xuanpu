"""停用流水线的后台收尾（飞书离职事件用）：任务引用、并发上限、按员工合并、关闭时的等待与取消。

用一条不依附平台的流水线（内存存储），监听者是测试里的替身。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from xuanpu_server.audit import AuditLog
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.identity import InMemoryIdentityStore, UserRecord
from xuanpu_server.identity.disable import (
    BACKGROUND_CONCURRENCY,
    FEISHU_EVENT_ACTOR,
    DisablePipeline,
)
from xuanpu_server.tenancy import Principal, Tenant, system_principal

if TYPE_CHECKING:
    from collections.abc import Callable

FEISHU = system_principal("acme", FEISHU_EVENT_ACTOR)
ADMIN = Principal(role="tenant_admin", tenant_id="acme", key_id="0123456789ab")


class Recorder:
    """记下每次被通知的监听者。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """记一笔。"""
        self.calls.append((tenant_id, user_id))


class Broken:
    """总是失败的监听者。"""

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """失败。"""
        raise RuntimeError("连不上")


class Gate:
    """等测试放行才结束的监听者；记下同时在跑的个数与峰值。"""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls: list[str] = []
        self.running = 0
        self.peak = 0

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """进门计数，等放行。"""
        self.calls.append(user_id)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.release.wait()
        finally:
            self.running -= 1


class Hanging:
    """一直不结束的监听者（直到被取消）。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """等到被取消。"""
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def standalone_pipeline(
    **kwargs: Any,
) -> tuple[DisablePipeline, UserRecord, InMemoryControlPlane]:
    """一条不依附平台的流水线（内存存储），外加一名员工与控制面（查审计）。"""
    control = InMemoryControlPlane()
    await control.create_tenant(Tenant(tenant_id="acme", name="Acme"))
    store = InMemoryIdentityStore(control)
    user = UserRecord(tenant_id="acme", user_id="usr_a", display_name="A", created_at=0.0)
    await store.create_user(user)
    return DisablePipeline(store=store, audit=AuditLog(control), **kwargs), user, control


async def wait_until(condition: Callable[[], bool], *, seconds: float = 5.0) -> None:
    """轮询直到条件成立；超时就让用例失败。"""
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "等不到条件成立"
        await asyncio.sleep(0.01)


async def test_background_notify_is_tracked_and_settled() -> None:
    """``notify_later`` 放后台：任务引用存在流水线里，``settle`` 等它们跑完；失败的监听者照样记
    审计。"""
    pipeline, user, control = await standalone_pipeline()
    recorder = Recorder()
    pipeline.register("broken", Broken())
    pipeline.register("recorder", recorder)

    pipeline.notify_later("acme", user.user_id, FEISHU)
    assert pipeline.pending == 1
    await pipeline.settle()

    assert pipeline.pending == 0
    assert recorder.calls == [("acme", user.user_id)]
    [failed] = await control.query_audit("acme", AuditQuery(kind="user_cleanup_failed"))
    assert (failed.actor, failed.user_id) == ("system:feishu-event", user.user_id)
    await pipeline.close()


async def test_background_concurrency_is_capped() -> None:
    """一阵离职事件：同时在跑收尾的最多 ``BACKGROUND_CONCURRENCY`` 个，别的排队；放行之后全部跑完。"""
    pipeline, _, _ = await standalone_pipeline()
    gate = Gate()
    pipeline.register("gate", gate)
    total = BACKGROUND_CONCURRENCY * 3
    for index in range(total):
        pipeline.notify_later("acme", f"usr_{index:02d}", FEISHU)

    await wait_until(lambda: gate.running == BACKGROUND_CONCURRENCY)
    await asyncio.sleep(0.05)  # 再给排队的一点机会：它们不该挤进来
    assert (gate.running, pipeline.pending) == (BACKGROUND_CONCURRENCY, total)

    gate.release.set()
    await pipeline.settle()
    assert gate.peak == BACKGROUND_CONCURRENCY
    assert sorted(gate.calls) == [f"usr_{index:02d}" for index in range(total)]
    await pipeline.close()


async def test_background_merges_requests_per_user() -> None:
    """同一名员工已有后台任务时不再新建：跑着的那一轮可能在新请求之前就列过他的会话，所以跑完再来
    一轮，这期间来的请求不论几个都并成这一轮（用最新请求的 ``actor``）；之后再来的照常新开一个。"""
    pipeline, user, control = await standalone_pipeline()
    gate = Gate()
    pipeline.register("gate", gate)
    pipeline.register("broken", Broken())

    pipeline.notify_later("acme", user.user_id, FEISHU)
    await wait_until(lambda: gate.running == 1)
    for _ in range(4):
        pipeline.notify_later("acme", user.user_id, ADMIN)
    assert pipeline.pending == 1

    gate.release.set()
    await pipeline.settle()
    assert gate.calls == [user.user_id] * 2
    failed = await control.query_audit("acme", AuditQuery(kind="user_cleanup_failed"))
    assert [record.actor for record in failed] == ["system:feishu-event", ADMIN.actor]

    pipeline.notify_later("acme", user.user_id, FEISHU)
    await pipeline.settle()
    assert gate.calls == [user.user_id] * 3
    await pipeline.close()


async def test_close_waits_then_cancels_background(caplog: Any) -> None:
    """关闭时等后台任务最多 ``drain_seconds``，还没完的取消并记告警；关闭之后不再接新的后台任务。"""
    pipeline, user, _ = await standalone_pipeline(drain_seconds=0.05)
    hanging = Hanging()
    pipeline.register("hanging", hanging)
    pipeline.notify_later("acme", user.user_id, FEISHU)
    await asyncio.wait_for(hanging.started.wait(), timeout=5)

    with caplog.at_level(logging.WARNING, logger="xuanpu_server.identity.disable"):
        await asyncio.wait_for(pipeline.close(), timeout=5)
        pipeline.notify_later("acme", user.user_id, FEISHU)

    assert hanging.cancelled
    assert pipeline.pending == 0
    assert "取消" in caplog.text
    assert "已关闭" in caplog.text
