"""派工后进程被终止：真实子进程在唤醒处理中途被 SIGKILL，重启后恰好交付一次。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.butler.service import ButlerMessageBody
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.tracing.models import TraceOwner

from .conftest import employee, enable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
WORKER = Path(__file__).resolve().parents[4] / "scripts/acceptance/r5_butler_wake_worker.py"


@pytest.fixture
async def pg_world(identity_stores: Any, world_opener: Any, local_sandboxes: Any,
                   ) -> AsyncIterator[Any]:
    """PostgreSQL 后端的平台；后台唤醒循环停掉，由用例与子进程显式驱动。"""
    control, identity = identity_stores
    if not isinstance(control, PostgresControlPlane):
        pytest.skip("真实进程故障只对 PostgreSQL 执行")
    world = await world_opener(store=control, identity_store=identity, sandboxes=local_sandboxes())
    try:
        await world.platform.butler.wakeup.close()
        yield world
    finally:
        await world.platform.close()


async def _dispatched(world: Any, step: Any) -> str:
    """员工说一句话、管家派工、匠人完成这一轮；返回派工标识。"""
    enable(world, wake_interval_seconds=300)
    await world.publish()
    alice = await employee(world)
    world.models.script_for("INSTR-K", step(text="工单 T-7 已处理完毕。"))
    world.models.script(step(calls=[("c1", "dispatch_artisan", {
        "package": "support-desk", "entry": "triage", "instruction": "INSTR-K 处理 T-7"})]),
        step(text="已交给匠人。"))
    receipt = await world.platform.butler.message(alice, ButlerMessageBody(
        request_id="k1", text="处理工单 T-7"))
    [record] = await world.platform.store.dispatch_store.list_for_trace(ALICE, receipt["trace_id"])
    async with asyncio.timeout(20):
        while not await world.platform.store.query_audit("acme", AuditQuery(
                session_id=record.host_ref, kind="turn_text", limit=10)):
            await asyncio.sleep(0.05)
    return str(record.dispatch_id)


async def _worker(world: Any, tmp_path: Path, dsn: str, phase: str) -> asyncio.subprocess.Process:
    """起子进程：读独占测试命名空间的私有配置。"""
    namespace = world.platform.store._namespace  # noqa: SLF001
    marker = tmp_path / "wake-marker.json"
    config = {"dsn": dsn, "schema": namespace.schema, "prefix": namespace.prefix, "tenant": "acme",
              "objects_dir": str(world.objects_dir), "phase": phase, "marker": str(marker)}
    path = tmp_path / "wake-worker.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return await asyncio.create_subprocess_exec(
        sys.executable, str(WORKER), "--config", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


@pytest.mark.parametrize("phase", ["after_decide", "after_message"])
async def test_sigkill_mid_wake_delivers_exactly_once(
    pg_world: Any, step: Any, tmp_path: Path, postgres_dsn: str, phase: str,
) -> None:
    """子进程处理唤醒到一半被杀；重启（本进程按持久记录续做）后只有一条交付、派工 delivered。"""
    world = pg_world
    dispatch_id = await _dispatched(world, step)
    child = await _worker(world, tmp_path, postgres_dsn, phase)
    try:
        async with asyncio.timeout(30):
            while not (tmp_path / "wake-marker.json").exists():
                if child.returncode is not None:
                    _, err = await child.communicate()
                    pytest.fail("子进程提前退出：" + err.decode()[-2000:])
                await asyncio.sleep(0.05)
        child.kill()
        await asyncio.wait_for(child.wait(), 10)
        assert child.returncode == -9
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    store = world.platform.store
    [event] = await store.wake_store.unacked("acme")
    assert event.decision is not None and event.decision.review is None
    assert (await store.dispatch_store.get(ALICE, dispatch_id)).state != "delivered"
    for _ in range(3):
        await world.platform.butler.wakeup.run_once()
    assert (await store.dispatch_store.get(ALICE, dispatch_id)).state == "delivered"
    assert await store.wake_store.unacked("acme") == ()
    messages = await world.platform.workspace.messages(
        (await employee_principal()), limit=100, latest=True, private_only=True)
    delivered = [m for m in messages if str(m["request_id"]).endswith(":deliver")]
    assert len(delivered) == 1 and "T-7 已处理完毕" in delivered[0]["text"]


async def employee_principal() -> Any:
    """Alice 的员工身份。"""
    from xuanpu_server.tenancy import Principal

    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id="usr_alice")
