"""能力结果统计接口（R4-F0）：员工本人与管理员两种视图。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI 解析响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 解析认证依赖
from xuanpu_server.outcomes.acceptance import (  # noqa: TC001 —— FastAPI 解析请求模型
    BatchInput,
    ObservationInput,
)

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

    @router.post('/v1/admin/acceptance-batches', status_code=201)
    async def create_batch(body: BatchInput, who: Caller) -> dict[str, JsonValue]:
        """冻结一个验收批次（仅管理员）。"""
        return await platform.acceptance.create(who, body)

    @router.get('/v1/acceptance-batches/{batch_id}')
    async def batch(batch_id: str, who: Caller) -> dict[str, JsonValue]:
        """批次详情与核实通过率（仅管理员）。"""
        return await platform.acceptance.detail(who, batch_id)

    @router.post('/v1/acceptance-batches/{batch_id}/observations', status_code=201)
    async def observe(batch_id: str, body: ObservationInput, who: Caller) -> dict[str, JsonValue]:
        """追加一条核实结论（不改任务、交接或审批状态）。"""
        return await platform.acceptance.observe(who, batch_id, body)

    return router
