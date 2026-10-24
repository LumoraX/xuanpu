"""员工原生选择共享来源，旧上下文失效时不补读正文。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI依赖
from xuanpu_server.shared_matters.schemas import (  # noqa: TC001 —— 请求模型
    CreateSharedContext,
    SharedChange,
)

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def shared_contexts_router(platform: Platform) -> APIRouter:
    """客户端只提供选择，员工身份和来源证明由服务器确定。"""
    router = APIRouter()

    @router.post('/v1/shared-contexts', status_code=201)
    async def create(body: CreateSharedContext, who: Caller) -> dict[str, JsonValue]:
        """原生确认固定来源后才创建上下文。"""
        return await platform.shared_contexts.create(who, body)

    @router.get('/v1/shared-contexts')
    async def listing(who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """本人历史阅读状态目录，支持结束不再使用的阅读。"""
        return await platform.shared_contexts.listing(who, limit=limit, cursor=cursor)

    @router.get('/v1/shared-contexts/{identifier}/read')
    async def read(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """当前权限与代次复核后补读本人来源快照。"""
        return await platform.shared_contexts.read(who, identifier)

    @router.get('/v1/shared-contexts/{identifier}')
    async def metadata(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """本人阅读状态与版本，不含失效正文。"""
        return await platform.shared_contexts.metadata(who, identifier)

    @router.post('/v1/shared-contexts/{identifier}/close')
    async def close(identifier: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """确认结束阅读后旧上下文保持终止。"""
        return await platform.shared_contexts.close(who, identifier, body)

    return router
