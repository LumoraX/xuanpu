"""组织原生接口不依赖模型，职责检查由同一领域服务执行。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI运行时解析响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI运行时解析认证依赖
from xuanpu_server.organization.schemas import (  # noqa: TC001 —— FastAPI运行时解析请求模型
    AuthorityInput,
    ConfirmInput,
    ImportApplyInput,
    ImportInput,
    MembershipInput,
    OrgChangeInput,
    PositionInput,
    RoleInput,
    UnitInput,
)

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform

Collection = Literal['units', 'positions', 'memberships', 'roles']

def organization_router(platform: Platform) -> APIRouter:
    """所有路由共用当前组织权限与全局Cookie写入保护。"""
    router = APIRouter()
    service = platform.organization

    @router.get('/v1/organization/people')
    async def people(who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """直接选择启用员工，不要求先配置部门或暴露私人资料。"""
        return await service.people(who, limit=limit, cursor=cursor)

    @router.post('/v1/admin/organization/import-previews', status_code=201)
    async def import_preview(body: ImportInput, who: Caller) -> dict[str, JsonValue]:
        """采集企微只读目录并生成固定差异。"""
        return await platform.organization_sync.create(who, body)

    @router.get('/v1/admin/organization/import-previews/{identifier}')
    async def import_get(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """显示来源范围、完整性、分页错误和冲突。"""
        return await platform.organization_sync.get(who, identifier)

    @router.post('/v1/admin/organization/import-previews/{identifier}/apply')
    async def import_apply(identifier: str, body: ImportApplyInput, who: Caller) -> dict[str, JsonValue]:
        """明确选中固定批次后重新核对来源及本地版本。"""
        return await platform.organization_sync.apply(who, identifier, body)

    @router.get('/v1/admin/organization/authority')
    async def authority(who: Caller) -> dict[str, JsonValue]:
        """查询字段来源规则。"""
        return await service.authority.listing(who)

    @router.post('/v1/admin/organization/authority/previews', status_code=201)
    async def authority_preview(body: AuthorityInput, who: Caller) -> dict[str, JsonValue]:
        """预览来源切换影响，不直接改变来源。"""
        return await service.authority.preview(who, body)

    @router.post('/v1/admin/organization/authority/previews/{identifier}/apply')
    async def authority_apply(identifier: str, body: ConfirmInput, who: Caller) -> dict[str, JsonValue]:
        """显式确认当前预览版本。"""
        return await service.authority.apply(who, identifier, body)

    @router.get('/v1/organization/me')
    async def me(who: Caller) -> dict[str, JsonValue]:
        """本人任职与业务职责。"""
        return await service.me(who)

    @router.get('/v1/organization/directory')
    async def directory(who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """当前可见的最小组织目录。"""
        return await service.directory(who, limit=limit, cursor=cursor)

    @router.post('/v1/admin/organization/units', status_code=201)
    async def create_unit(body: UnitInput, who: Caller) -> dict[str, JsonValue]:
        """新建部门。"""
        return await service.create(who, 'units', body)

    @router.post('/v1/admin/organization/positions', status_code=201)
    async def create_position(body: PositionInput, who: Caller) -> dict[str, JsonValue]:
        """新建部门岗位。"""
        return await service.create(who, 'positions', body)

    @router.post('/v1/admin/organization/memberships', status_code=201)
    async def create_membership(body: MembershipInput, who: Caller) -> dict[str, JsonValue]:
        """显式授予任职关系。"""
        return await service.create(who, 'memberships', body)

    @router.post('/v1/admin/organization/roles', status_code=201)
    async def create_role(body: RoleInput, who: Caller) -> dict[str, JsonValue]:
        """显式授予本地业务职责。"""
        return await service.create(who, 'roles', body)

    @router.get('/v1/admin/organization/{kind}')
    async def listing(kind: Collection, who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """管理范围内列表与独立统计。"""
        return await service.listing(who, kind, limit=limit, cursor=cursor)

    @router.get('/v1/admin/organization/{kind}/{identifier}')
    async def get(kind: Collection, identifier: str, who: Caller) -> dict[str, JsonValue]:
        """越界对象与不存在对象统一404。"""
        return await service.listing(who, kind, identifier=identifier)

    @router.patch('/v1/admin/organization/{kind}/{identifier}')
    async def change(kind: Collection, identifier: str, body: OrgChangeInput, who: Caller) -> dict[str, JsonValue]:
        """以当前版本提交变更，关闭灰度后保留必要撤销。"""
        return await service.change(who, kind, identifier, body)

    return router
