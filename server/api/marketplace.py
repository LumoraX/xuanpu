"""能力市场接口：浏览匠人市场与通用技能市场、按依赖拓扑一键安装到租户。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析依赖
from server.api.localization import request_locale

if TYPE_CHECKING:
    from server.platform import Platform


class InstallMarketplaceBody(BaseModel):
    """安装市场能力的请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=63)
    activate: bool = True


def marketplace_router(platform: Platform) -> APIRouter:
    """挂载能力市场路由（匠人市场 + 通用技能市场）。"""
    router = APIRouter()

    @router.get("/v1/marketplace/items")
    async def list_marketplace_items(
        request: Request,
        who: Caller,
        kind: Annotated[Literal["all", "artisan", "skill_library"], Query()] = "all",
        category: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        """列出能力市场中的匠人与通用技能库，附带当前租户的安装状态与待配置密钥。"""
        return await platform.marketplace.list_items(
            who,
            kind=kind,
            category=category,
            query=q,
            locale=request_locale(request),
        )

    @router.post("/v1/marketplace/install", status_code=201)
    async def install_marketplace_item(
        request: Request,
        body: InstallMarketplaceBody,
        who: Caller,
    ) -> dict[str, Any]:
        """一键安装市场中的匠人或技能库，自动先安装所依赖的通用技能库。"""
        return await platform.marketplace.install(
            who,
            body.name,
            activate=body.activate,
            locale=request_locale(request),
        )

    return router


__all__ = ["InstallMarketplaceBody", "marketplace_router"]
