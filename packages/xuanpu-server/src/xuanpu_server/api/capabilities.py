"""能力接口与既有Cookie保护共用；R4严格输入独立返回422。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import JsonValue  # noqa: TC002 —— FastAPI解析响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI解析认证依赖
from xuanpu_server.capabilities.schemas import (  # noqa: TC001 —— FastAPI解析请求模型
    AssignInput,
    EvaluateInput,
    InstallInput,
    PreviewInput,
    RegisterInput,
    ReleaseInput,
    ReleasePreviewInput,
    ReleaseStateInput,
    RevokeInput,
    StateInput,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from starlette.requests import Request
    from starlette.responses import Response

    from xuanpu_server.platform import Platform


class CapabilityRoute(APIRoute):
    """输入错误不回显输入值，兼容其他旧接口既有400契约。"""
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """在本路由内处理验证异常，不扩大全局异常处理范围。"""
        handler = super().get_route_handler()

        async def validated(request: Request) -> Response:
            """只返回固定提示，未知字段可能夹带秘密。"""
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={'error': {
                    'code': 'invalid_delivery_input', 'message': '能力配置参数不合法，请核对格式与声明字段'}})
        return validated


def capabilities_router(platform: Platform) -> APIRouter:
    """只暴露登记和只读目录，不开放执行。"""
    router = APIRouter(route_class=CapabilityRoute)

    @router.post('/v1/capabilities', status_code=201)
    async def register(body: RegisterInput, who: Caller) -> dict[str, JsonValue]:
        """登记精确包修订，不切换包活动指针。"""
        return await platform.capabilities.register(who, body)

    @router.get('/v1/admin/capability-drain')
    async def drain(package: str, who: Caller) -> dict[str, JsonValue]:
        """首次纳管前的排空预览：这个包没有能力依据的在途会话数（只读，仅管理员）。"""
        from xuanpu_server.capabilities.drain import drain_preview

        return await drain_preview(platform.capabilities, who, package)

    @router.get('/v1/capabilities')
    async def listing(who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """仅管理员可枚举配置记录。"""
        return await platform.capabilities.listing(who, limit=limit, cursor=cursor)

    @router.get('/v1/me/capabilities')
    async def mine(who: Caller) -> dict[str, JsonValue]:
        """本人只读可运行目录，当前阶段明确空集合。"""
        return await platform.capabilities.mine(who)

    @router.post('/v1/capabilities/{identifier}/install-previews', status_code=201)
    async def preview(identifier: str, body: PreviewInput, who: Caller) -> dict[str, JsonValue]:
        """保存固定预检，尚不改变当前安装。"""
        return await platform.capabilities.installations.preview(who, identifier, body)

    @router.post('/v1/capabilities/{identifier}/installations', status_code=201)
    async def install(identifier: str, body: InstallInput, who: Caller) -> dict[str, JsonValue]:
        """确认条件后仅保存staged安装。"""
        return await platform.capabilities.installations.install(who, identifier, body)

    @router.post('/v1/installations/{identifier}/state')
    async def change(identifier: str, body: StateInput, who: Caller) -> dict[str, JsonValue]:
        """停止动作不删除历史，当前阶段拒绝ready。"""
        return await platform.capabilities.installations.change(who, identifier, body)

    @router.post('/v1/capabilities/{identifier}/assignments', status_code=201)
    async def assign(identifier: str, body: AssignInput, who: Caller) -> dict[str, JsonValue]:
        """把能力的明确修订分配给员工（或可选的岗位/部门）；只授权使用能力。"""
        return await platform.capabilities.assignments.assign(who, identifier, body)

    @router.get('/v1/capabilities/{identifier}/assignments')
    async def assignments(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """某能力的全部分配（含已撤销），仅管理员。"""
        return await platform.capabilities.assignments.listing(who, identifier)

    @router.post('/v1/capability-assignments/{identifier}/revoke')
    async def revoke(identifier: str, body: RevokeInput, who: Caller) -> dict[str, JsonValue]:
        """按版本撤销分配，下一次调用即被拒。"""
        return await platform.capabilities.assignments.revoke(who, identifier, body)

    @router.post('/v1/capability-evaluations', status_code=202)
    async def evaluate(body: EvaluateInput, who: Caller) -> dict[str, JsonValue]:
        """受理一次隔离评测，后台逐用例运行；结果经查询接口读取。"""
        return await platform.capabilities.evaluations.start(who, body)

    @router.get('/v1/capability-evaluations/{identifier}')
    async def evaluation(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """评测状态与逐用例判定（仅管理员）。"""
        return await platform.capabilities.evaluations.get(who, identifier)

    @router.post('/v1/capabilities/{identifier}/release-previews')
    async def release_preview(identifier: str, body: ReleasePreviewInput, who: Caller) -> dict[str, JsonValue]:
        """只读发布预览：证据核对、名单变化与确认摘要。"""
        return await platform.capabilities.releases.preview(who, identifier, body)

    @router.post('/v1/capabilities/{identifier}/releases', status_code=201)
    async def release(identifier: str, body: ReleaseInput, who: Caller) -> dict[str, JsonValue]:
        """按预览摘要确认发布给固定员工名单。"""
        return await platform.capabilities.releases.release(who, identifier, body)

    @router.get('/v1/capabilities/{identifier}/releases')
    async def releases(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """某能力的全部发布（仅管理员）。"""
        return await platform.capabilities.releases.listing(who, identifier)

    @router.post('/v1/capability-releases/{identifier}/retire')
    async def retire(identifier: str, body: ReleaseStateInput, who: Caller) -> dict[str, JsonValue]:
        """正常停止：不再新受理，已绑定工作按原授权收尾。"""
        return await platform.capabilities.releases.change(who, identifier, body, 'retired')

    @router.post('/v1/capability-releases/{identifier}/revoke')
    async def revoke_release(identifier: str, body: ReleaseStateInput, who: Caller) -> dict[str, JsonValue]:
        """安全撤销：代次加一，已绑定工作也不能继续。"""
        return await platform.capabilities.releases.change(who, identifier, body, 'revoked')

    return router
