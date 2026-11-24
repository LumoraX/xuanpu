"""管家测试的共用装配：开启管家与追踪、员工、HTTP 客户端。"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.engine.butler.config import ButlerConfig
from server.engine.tracing.config import ExecutionTraceConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def enable(world: Any, *, butler: bool = True, **limits: Any) -> None:
    """测试内装配：追踪对 acme 开启，管家按参数开启。"""
    platform = world.platform
    platform.runtimes._defaults = replace(  # noqa: SLF001 —— 测试内装配
        platform.runtimes.defaults,
        execution_trace=ExecutionTraceConfig(enabled=True, tenants=("acme",)))
    platform.butler_config = ButlerConfig(enabled=butler, tenants=("acme",), **limits)


async def employee(world: Any, user_id: str = "usr_alice") -> Principal:
    """建一名在职员工。"""
    await world.platform.identity.store.create_user(UserRecord(
        tenant_id="acme", user_id=user_id, display_name=user_id, created_at=1.0))
    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id=user_id)


@pytest.fixture
async def butler(world: Any) -> AsyncIterator[dict[str, Any]]:
    """开启管家的平台、已发布的样例匠人、员工 Alice 与以她身份调用的 HTTP 客户端。"""
    enable(world)
    await world.publish()
    alice = await employee(world)
    app = create_app(world.platform)
    current: dict[str, Principal] = {"who": alice}
    app.dependency_overrides[caller] = lambda: current["who"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://x") as http:
        yield {"http": http, "as": current, "alice": alice, "world": world}
