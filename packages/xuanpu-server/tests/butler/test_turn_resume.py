"""中断后的一轮由唤醒重跑（R6-D，ADR 0020）：持有者已不在（租约过期仍认领中）的轮次按同一
请求标识重跑一次；重跑过仍中断的写明没处理完并释放；失败释放的与旧记录不重跑。内存与 PG 各跑一遍。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from .conftest import employee, enable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

INPUT = {"text": "整理上周的客户反馈", "timezone": "Asia/Shanghai", "input_files": []}


@pytest.fixture
async def any_world(identity_stores: Any, world_opener: Any, local_sandboxes: Any,
                    ) -> AsyncIterator[Any]:
    """内存或 PostgreSQL 后端的平台；后台唤醒循环停掉，由用例显式驱动。"""
    control, identity = identity_stores
    world = await world_opener(store=control, identity_store=identity, sandboxes=local_sandboxes())
    try:
        await world.platform.butler.wakeup.close()
        yield world
    finally:
        await world.platform.close()


async def _crashed_claim(world: Any, alice: Any, request_id: str, *, resumes: int = 0,
                         with_input: bool = True) -> str:
    """模拟进程中途退出：认领并记下输入后不释放，返回认领令牌。"""
    workspace = world.platform.workspace
    await workspace.append_message(alice, "user", INPUT["text"], request_id + ":user")
    claim = await workspace.claim_route(alice, "butler:" + request_id, INPUT)
    if with_input:
        await workspace.annotate_route(alice, "butler:" + request_id, claim["token"],
                                       {"butler_input": INPUT, "resumes": resumes})
    return str(claim["token"])


def _after_lease(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """时钟拨到租约（60 秒）之后。"""
    later = time.time() + 120
    monkeypatch.setattr(world.platform.workspace, "_now", lambda: later)


async def _replies(world: Any, alice: Any, request_id: str) -> list[str]:
    """主对话里这句话的管家回复。"""
    found = await world.platform.workspace.messages(alice, limit=100, latest=True)
    return [m["text"] for m in found if m["role"] == "assistant"
            and str(m.get("request_id", "")).startswith(request_id)]


async def _drain(world: Any) -> None:
    """等后台轮次跑完。"""
    import asyncio

    running = [r.task for r in world.platform.butler._running.values()]  # noqa: SLF001
    await asyncio.gather(*running, return_exceptions=True)


async def test_interrupted_turn_is_rerun_once(any_world: Any, step: Any,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """中断的一轮：唤醒按同一标识重跑，回复进主对话，认领完成；再对账不会重跑。"""
    world = any_world
    enable(world)
    alice = await employee(world)
    await _crashed_claim(world, alice, "r1")
    _after_lease(world, monkeypatch)
    world.models.script(step(text="反馈整理好了。"))
    assert await world.platform.butler.resume_stale("acme") == 1
    await _drain(world)
    assert await _replies(world, alice, "r1") == ["反馈整理好了。"]
    assert await world.platform.butler.resume_stale("acme") == 0


async def test_turn_interrupted_twice_is_reported_not_rerun(
    any_world: Any, step: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重跑过仍中断：不再重跑，主对话写明没处理完、原话保留，认领释放后不再被捡起。"""
    world = any_world
    enable(world)
    alice = await employee(world)
    await _crashed_claim(world, alice, "r2", resumes=1)
    _after_lease(world, monkeypatch)
    assert await world.platform.butler.resume_stale("acme") == 0
    [reply] = await _replies(world, alice, "r2")
    assert "原话已保留" in reply
    assert await world.platform.workspace.stale_routes("acme", "butler:") == []
    assert world.models.requests == []


async def test_released_and_legacy_claims_are_left_alone(
    any_world: Any, step: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败后已释放的认领（租约 0）与没记输入的旧认领：不重跑。"""
    world = any_world
    enable(world)
    alice = await employee(world)
    token = await _crashed_claim(world, alice, "r3")
    await world.platform.workspace.release_route(alice, "butler:r3", token)
    await _crashed_claim(world, alice, "r4", with_input=False)
    _after_lease(world, monkeypatch)
    assert await world.platform.butler.resume_stale("acme") == 0
    assert world.models.requests == []
