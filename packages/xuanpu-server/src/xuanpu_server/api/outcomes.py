"""能力结果统计接口（R4-F0）：员工本人与管理员两种视图。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI 解析响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 解析认证依赖

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def outcomes_router(platform: Platform) -> APIRouter:
    """统计每次现算；分页不改变总数。"""
    router = APIRouter()

    @router.get('/v1/me/capability-outcomes')
    async def mine(who: Caller, since: float | None = None, until: float | None = None,
                   limit: int = 50, offset: int = 0) -> dict[str, JsonValue]:
        """本人能力任务的状态、完成率与用量。"""
        return await platform.outcomes.mine(who, since=since, until=until, limit=limit, offset=offset)

    @router.get('/v1/admin/capability-outcomes')
    async def admin(who: Caller, since: float | None = None, until: float | None = None) -> dict[str, JsonValue]:
        """按能力与修订的计数与用量（仅管理员，不含正文）。"""
        return await platform.outcomes.admin(who, since=since, until=until)

    return router
