"""共享原生接口无需模型，Cookie写入复用全局CSRF保护。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI依赖
from xuanpu_server.api.shared_materials import shared_materials_router
from xuanpu_server.shared_matters.schemas import (  # noqa: TC001 —— FastAPI请求模型
    ChangeMatterInput,
    CreateMatterInput,
    SharedChange,
    SharedMemberInput,
    TransferOwnerInput,
)

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def shared_matters_router(platform: Platform) -> APIRouter:
    """固定端点不允许请求自行选择租户或执行员工。"""
    router = APIRouter(prefix='/v1/shared-matters')
    service = platform.shared_matters
    router.include_router(shared_materials_router(platform))

    @router.post('', status_code=201)
    async def create(body: CreateMatterInput, who: Caller) -> dict[str, JsonValue]:
        """明确创建独立共享事项。"""
        return await service.create(who, body)

    @router.get('')
    async def listing(who: Caller, search: str = '', limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """当前授权投影的列表、搜索和独立统计。"""
        return await service.listing(who, search=search, limit=limit, cursor=cursor)

    @router.get('/{identifier}')
    async def get(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """读取当前授权范围。"""
        return await service.get(who, identifier)

    @router.patch('/{identifier}')
    async def change(identifier: str, body: ChangeMatterInput, who: Caller) -> dict[str, JsonValue]:
        """CAS编辑，关闭事项不代表签收。"""
        return await service.change(who, identifier, body)

    @router.get('/{identifier}/members')
    async def members(identifier: str, who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """限定资料成员不可枚举事项参与者。"""
        return await service.members.listing(who, identifier, limit=limit, cursor=cursor)

    @router.post('/{identifier}/members', status_code=201)
    async def invite(identifier: str, body: SharedMemberInput, who: Caller) -> dict[str, JsonValue]:
        """明确指定员工或可选部门。"""
        return await service.members.add(who, identifier, body)

    @router.post('/{identifier}/members/{member_id}/revoke')
    async def revoke(identifier: str, member_id: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """撤销当前授权并使旧来源证明失效。"""
        return await service.members.revoke(who, identifier, member_id, body)

    @router.post('/{identifier}/transfer-owner')
    async def transfer(identifier: str, body: TransferOwnerInput, who: Caller) -> dict[str, JsonValue]:
        """负责人明确转交，不能由管理员接管。"""
        return await service.members.transfer(who, identifier, body)

    @router.get('/{identifier}/events')
    async def history(identifier: str, who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """历史正文仍按当前完整事项权限访问。"""
        return await service.history(who, identifier, limit=limit, cursor=cursor)

    return router
