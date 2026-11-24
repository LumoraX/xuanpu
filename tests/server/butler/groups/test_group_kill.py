"""编组的真实进程故障（R5 总门槛）：子进程推进编组到一半被 SIGKILL，重启后
不重复派工、不重复汇总与裁决项，执行树仍能读出。只对 PostgreSQL 执行。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller
from server.core.controlplane.models import AuditQuery
from server.core.controlplane.postgres import PostgresControlPlane

from ..conftest import employee, enable
from .helpers import ALICE, WAKE, group, member, member_dispatches, plan, submit, summaries, until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

WORKER = Path(__file__).resolve().parents[4] / "scripts/acceptance/r5_group_kill_worker.py"
COMPARE = "consult_compare"


@pytest.fixture
async def pg_butler(identity_stores: Any, world_opener: Any, local_sandboxes: Any,
                    ) -> AsyncIterator[dict[str, Any]]:
    """PostgreSQL 后端、开启管家的平台；后台唤醒循环停掉，由用例与子进程显式驱动。"""
    control, identity = identity_stores
    if not isinstance(control, PostgresControlPlane):
        pytest.skip("真实进程故障只对 PostgreSQL 执行")
    world = await world_opener(store=control, identity_store=identity, sandboxes=local_sandboxes())
    try:
        await world.platform.butler.wakeup.close()
        enable(world, wake_interval_seconds=300)
        await world.publish()
        alice = await employee(world)
        app = create_app(world.platform)
        app.dependency_overrides[caller] = lambda: alice
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://x") as http:
            yield {"http": http, "alice": alice, "world": world}
    finally:
        await world.platform.close()


async def _kill_at(world: Any, tmp_path: Path, dsn: str, phase: str, **extra: Any) -> None:
    """起子进程推进，等它写出阶段标记后 SIGKILL。"""
    namespace = world.platform.store._namespace  # noqa: SLF001
    marker = tmp_path / "group-marker.json"
    config = {"dsn": dsn, "schema": namespace.schema, "prefix": namespace.prefix, "tenant": "acme",
              "user": "usr_alice", "objects_dir": str(world.objects_dir), "phase": phase,
              "marker": str(marker), **extra}
    path = tmp_path / "group-worker.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    child = await asyncio.create_subprocess_exec(
        sys.executable, str(WORKER), "--config", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(30):
            while not marker.exists():
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


async def _rounds_finished(world: Any, trace_id: str) -> None:
    """等成员各自的一轮在会话里结束（不经唤醒）。"""
    async with asyncio.timeout(20):
        for record in await member_dispatches_world(world, trace_id):
            while not await world.platform.store.query_audit("acme", AuditQuery(
                    session_id=record.host_ref, kind="turn_text", limit=10)):
                await asyncio.sleep(0.05)


async def member_dispatches_world(world: Any, trace_id: str) -> list[Any]:
    """这条 trace 下的成员派工。"""
    records = await world.platform.dispatches.list_for_trace(ALICE, trace_id)
    return [r for r in records if r.group_id is not None]


async def test_kill_after_member_completes(
    pg_butler: dict[str, Any], step: Any, tmp_path: Path, postgres_dsn: str,
) -> None:
    """并行两位成员完成后，子进程登记了一位的结果即被杀：重启后不多派、只汇总一次、树可读。"""
    world = pg_butler["world"]
    world.models.script_for("INSTR-K1", step(text="K1：完成。"))
    world.models.script_for("INSTR-K2", step(text="K2：完成。"))
    world.models.script_for(WAKE, *[step(text="结果回答了原请求。") for _ in range(2)])
    receipt = await submit(pg_butler, step, "k1", plan(
        {"mode": "parallel", "members": [member("INSTR-K1"), member("INSTR-K2")]}))
    [group_id] = receipt["groups"]
    await _rounds_finished(world, receipt["trace_id"])
    await _kill_at(world, tmp_path, postgres_dsn, "after_member_result")
    record = await group(pg_butler, group_id)
    assert sum(run.passed is not None for run in record.runs) == 1, "故障点：只登记了一位"
    assert not record.summarized

    async def done() -> bool:
        return bool((await group(pg_butler, group_id)).summarized)

    await until(pg_butler, done)
    for _ in range(3):
        await world.platform.butler.wakeup.run_once()
    assert len(await member_dispatches(pg_butler, receipt["trace_id"])) == 2
    assert len(await summaries(pg_butler)) == 1
    assert await world.platform.store.wake_store.unacked("acme") == ()
    view = await world.platform.store.trace_store.get(ALICE, receipt["trace_id"])
    assert len([s for s in view.spans if s.kind == "group"]) == 1
    assert len([s for s in view.spans if s.kind == "dispatch"]) == 2


async def test_kill_before_ruling_is_recorded(
    pg_butler: dict[str, Any], step: Any, tmp_path: Path, postgres_dsn: str, monkeypatch: Any,
) -> None:
    """会诊高分歧：子进程写了裁决项与消息、编组尚未记下即被杀；重启后只有一个裁决项与一条
    裁决消息，裁决后只汇总一次，不重新比对、不多派工。"""
    world = pg_butler["world"]
    world.models.script_for(COMPARE, step(calls=[("r1", "rate_divergence", {"level": "high"})]),
                            step(text="已比对。"))
    world.models.script_for("INSTR-V1", step(text="V1：方案甲。"))
    world.models.script_for("INSTR-V2", step(text="V2：方案乙。"))
    world.models.script_for(WAKE, *[step(text="结果回答了原请求。") for _ in range(2)])
    consult = world.platform.butler.groups._consult  # noqa: SLF001 —— 测试内装配

    async def not_yet(*_args: Any, **_kwargs: Any) -> Any:
        """父进程在建裁决项前停下，把这一步留给子进程。"""
        raise RuntimeError("留给子进程")

    monkeypatch.setattr(consult, "open_ruling", not_yet)
    receipt = await submit(pg_butler, step, "v1", plan(
        {"mode": "consult", "members": [member("INSTR-V1", "怎么处理？"), member("INSTR-V2", "怎么处理？")]}))
    [group_id] = receipt["groups"]

    async def judged() -> bool:
        verdict = (await group(pg_butler, group_id)).verdict(0)
        return verdict is not None and verdict.pending_id is None

    await until(pg_butler, judged)
    monkeypatch.undo()
    await _kill_at(world, tmp_path, postgres_dsn, "before_ruling", group_id=group_id)
    assert (await group(pg_butler, group_id)).verdict(0).pending_id is None
    await world.platform.butler.groups.advance(ALICE, group_id)
    await world.platform.butler.groups.advance(ALICE, group_id)
    items = [i for i in await world.platform.store.wake_store.list_pending(ALICE) if i.kind == "ruling"]
    assert len(items) == 1
    assert (await group(pg_butler, group_id)).verdict(0).pending_id == items[0].pending_id
    messages = await world.platform.workspace.messages(
        pg_butler["alice"], limit=100, latest=True, private_only=True)
    assert len([m for m in messages if ":ruling:" in str(m["request_id"])]) == 1
    answer = await pg_butler["http"].post(f"/v1/assistant/pending/{items[0].pending_id}/answer",
                                          json={"request_id": "rule-1", "text": "采用方案甲"})
    assert answer.status_code == 200, answer.text

    async def done() -> bool:
        return bool((await group(pg_butler, group_id)).summarized)

    await until(pg_butler, done)
    assert len(await member_dispatches(pg_butler, receipt["trace_id"])) == 2
    assert len(await summaries(pg_butler)) == 1
    view = await world.platform.store.trace_store.get(ALICE, receipt["trace_id"])
    assert len([s for s in view.spans if s.kind == "dispatch"]) == 2
