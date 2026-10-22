"""连接原生入口；本人同意不经过模型批准。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI运行时解析员工身份依赖
from xuanpu_server.connections.account_bindings import bind
from xuanpu_server.connections.models import (  # noqa: TC001 —— FastAPI运行时解析请求体
    AccountBindingInput,
    ChangeInput,
    ConsentInput,
    InstallInput,
    PackageBindingInput,
    ReadInput,
    RevokeInput,
)
from xuanpu_server.connections.package_bindings import bind as bind_package

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def connections_router(platform: Platform) -> APIRouter:
    """登记安装、本人授权、撤销等权限一致的接口。"""
    router=APIRouter()
    service=platform.connections

    @router.get('/v1/me/connections')
    async def listing(who: Caller) -> dict[str,Any]:
        """当前本人安装与委托元数据。"""
        return await service.listing(who)

    @router.get('/v1/admin/connections')
    async def admin_listing(who: Caller) -> dict[str,Any]:
        """管理员只能读取安装元数据及自身委托。"""
        return await service.listing(who,admin=True)

    @router.post('/v1/admin/connections',status_code=201)
    async def install(body: InstallInput,who: Caller) -> dict[str,Any]:
        """创建尚未核定的自建应用。"""
        return await service.install(who,body)

    @router.patch('/v1/admin/connections/{cid}')
    async def change(cid: str,body: ChangeInput,who: Caller) -> dict[str,Any]:
        """显式核定能力后启用，停用仍可在灰度关闭后操作。"""
        return await service.change(who,cid,body)

    @router.post('/v1/me/connections/{cid}/consents',status_code=201)
    async def consent(cid: str,body: ConsentInput,who: Caller) -> dict[str,Any]:
        """本人浏览器原生确认。"""
        return await service.consent(who,cid,body)

    @router.get('/v1/me/connections/{cid}/consents/{consent_id}')
    async def consent_get(cid: str,consent_id: str,who: Caller) -> dict[str,Any]:
        """只取当前员工同意。"""
        return await service.consent_get(who,cid,consent_id)

    @router.post('/v1/me/connections/{cid}/consents/{consent_id}/revoke')
    async def revoke(cid: str,consent_id: str,body: RevokeInput,who: Caller) -> dict[str,Any]:
        """撤销保存后旧epoch不能继续。"""
        return await service.revoke(who,cid,consent_id,body)

    @router.post('/v1/me/connections/{cid}/read')
    async def read(cid: str,body: ReadInput,who: Caller) -> dict[str,Any]:
        """受保护只读请求，当前员工的委托和外部身份缺一不可。"""
        return await platform.connection_broker.read(who,cid,body.operation,body.parameters)

    @router.post('/v1/admin/connections/{cid}/accounts',status_code=201)
    async def account(cid: str,body: AccountBindingInput,who: Caller) -> dict[str,Any]:
        """管理员明确映射现有平台员工。"""
        return await bind(service,who,cid,body)

    @router.post('/v1/admin/connections/{cid}/package-bindings',status_code=201)
    async def package_binding(cid: str,body: PackageBindingInput,who: Caller) -> dict[str,Any]:
        """核定不可变包的只读操作。"""
        return await bind_package(service,who,cid,body)

    return router
