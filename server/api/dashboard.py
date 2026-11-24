"""员工概览接口（R5-E0）：``GET /v1/dashboard``，只读，不调模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析依赖
from server.api.localization import request_locale

if TYPE_CHECKING:
    from server.platform import Platform


def dashboard_router(platform: Platform) -> APIRouter:
    """注册本人概览路由。"""
    router = APIRouter()
    service = platform.dashboard

    @router.get("/v1/dashboard")
    async def overview(
        request: Request,
        who: Caller, timezone: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    ) -> dict[str, Any]:
        """四块概览；未开启的块不出现，读取失败的块单独标记。"""
        return await service.overview(who, timezone=timezone, locale=request_locale(request))

    return router
