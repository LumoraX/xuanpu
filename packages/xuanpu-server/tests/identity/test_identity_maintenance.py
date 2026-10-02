"""身份域的周期任务：清理过期的登录尝试与登录会话；出错不退出；随平台启停。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.identity import (
    InMemoryIdentityStore,
    LoginAttempt,
    LoginSession,
    UserRecord,
    new_user_id,
)
from xuanpu_server.identity.maintenance import PURGE_INTERVAL_SECONDS, IdentityMaintenance
from xuanpu_server.tenancy import QuotaLimits, Tenant

if TYPE_CHECKING:
    import pytest


async def _store_with_expired_and_live() -> tuple[InMemoryIdentityStore, str, str]:
    """一个身份存储：租户 acme 里一名员工、过期与未过期的登录尝试与登录会话各一条。

    返回 ``(存储, 未过期的 state, 未过期的令牌摘要)``。
    """
    control = InMemoryControlPlane()
    await control.create_tenant(
        Tenant(tenant_id="acme", name="Acme", quota=QuotaLimits(), created_at=time.time())
    )
    store = InMemoryIdentityStore(control)
    now = time.time()
    user = UserRecord(tenant_id="acme", user_id=new_user_id(), display_name="张三", created_at=now)
    await store.create_user(user)
    for name, expires_at in (("old", now - 1), ("live", now + 600)):
        await store.create_login_attempt(
            LoginAttempt(
                state=f"state-{name}",
                tenant_id="acme",
                code_verifier="v" * 43,
                return_to="/console/",
                created_at=now - 700,
                expires_at=expires_at,
                cookie_hash="c" * 64,
            )
        )
        await store.create_login_session(
            LoginSession(
                token_hash=f"hash-{name}",
                tenant_id="acme",
                user_id=user.user_id,
                created_at=now - 700,
                expires_at=expires_at,
                last_seen_at=now - 700,
            )
        )
    return store, "state-live", "hash-live"


def test_interval_is_five_minutes() -> None:
    """默认每 5 分钟清理一次。"""
    assert PURGE_INTERVAL_SECONDS == 300


async def test_run_once_purges_only_expired_logins() -> None:
    """一轮清理：过期的登录尝试与登录会话被删掉，没过期的留着。"""
    store, live_state, live_hash = await _store_with_expired_and_live()
    maintenance = IdentityMaintenance(store)
    assert await maintenance.run_once() == 2
    assert await maintenance.run_once() == 0
    now = time.time()
    assert await store.get_login_session(live_hash, now=now) is not None
    assert await store.take_login_attempt(live_state, now=now) is not None


async def test_loop_purges_periodically_and_survives_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """周期任务按间隔一轮一轮跑；某一轮出错只记日志，下一轮照常；关闭后不再跑。"""
    store, _, _ = await _store_with_expired_and_live()
    calls: list[float] = []
    original = store.purge_expired_logins

    async def flaky(now: float) -> int:
        """第一轮出错，之后照常清理。"""
        calls.append(now)
        if len(calls) == 1:
            raise ConnectionError("库暂时连不上")
        return await original(now)

    store.purge_expired_logins = flaky  # type: ignore[method-assign]
    maintenance = IdentityMaintenance(store, interval_seconds=0.01)
    maintenance.start()
    maintenance.start()  # 已经在跑，不会再起一个
    assert maintenance.running
    async with asyncio.timeout(5):
        while len(calls) < 3:
            await asyncio.sleep(0.01)
    await maintenance.close()
    assert not maintenance.running
    stopped_at = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == stopped_at
    assert "清理过期的登录尝试与登录会话失败" in caplog.text
    assert await original(time.time()) == 0  # 出错之后的那几轮已经清干净了
    await maintenance.close()  # 重复关闭也可以


async def test_runs_with_the_platform(world_opener: Any, local_sandboxes: Any) -> None:
    """平台启动时周期任务跟着启动，平台关闭时被取消并等到结束。"""
    world = await world_opener(store=InMemoryControlPlane(), sandboxes=local_sandboxes())
    maintenance = world.platform.identity.maintenance
    try:
        assert maintenance.running
    finally:
        await world.platform.close()
    assert not maintenance.running
