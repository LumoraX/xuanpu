"""概览测试的共用构造：两名员工、HTTP 客户端与可替换的调用身份。"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.engine.tracing.config import ExecutionTraceConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def employee(world: Any, user_id: str) -> Principal:
    """建一名员工并以本人身份调用。"""
    await world.platform.identity.store.create_user(UserRecord(
        tenant_id="acme", user_id=user_id, display_name=user_id, created_at=1.0))
    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id=user_id)


def enable_trace(world: Any, *tenants: str) -> None:
    """测试内替换平台默认参数：开启执行追踪名单。"""
    runtimes = world.platform.runtimes
    runtimes._defaults = replace(  # noqa: SLF001 —— 测试内装配
        runtimes.defaults, execution_trace=ExecutionTraceConfig(enabled=True, tenants=tenants))


@pytest.fixture
async def board(world: Any) -> AsyncIterator[dict[str, Any]]:
    """Alice 与 Bob 两名员工，以及一个可切换身份的 HTTP 客户端。"""
    alice = await employee(world, "usr_alice")
    bob = await employee(world, "usr_bob")
    app = create_app(world.platform)
    current: dict[str, Principal] = {"who": alice}
    app.dependency_overrides[caller] = lambda: current["who"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        yield {"http": http, "as": current, "alice": alice, "bob": bob, "world": world}


async def overview(board: dict[str, Any], who: str = "alice") -> dict[str, Any]:
    """以指定员工读取概览（UTC），按块名索引。"""
    board["as"]["who"] = board[who]
    response = await board["http"].get("/v1/dashboard", params={"timezone": "UTC"})
    assert response.status_code == 200, response.text
    body = response.json()
    return {**body, "by_key": {block["key"]: block for block in body["blocks"]}}
