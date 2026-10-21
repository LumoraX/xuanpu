"""员工主动业务跟进查询与明确决定入口，沿用登录 CSRF。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

from xuanpu_server.api.auth import Caller  # noqa: TC001
from xuanpu_server.proactive.models import ActionInput  # noqa: TC001

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def proactive_router(platform: Platform) -> APIRouter:
    """所有请求只访问当前员工，不能提交身份或直接运行模型。"""
    router = APIRouter(prefix='/v1/proactive')

    @router.get('')
    async def listing(who: Caller) -> dict[str, Any]:
        """返回持久关注事项和本人检查开关。"""
        settings = await platform.workspace.settings(who)
        return {'items': await platform.proactive.list(who),
                'enabled': settings.get('proactive_checks', {}).get('enabled', True)}

    @router.get('/{record_id}')
    async def read(record_id: str, who: Caller) -> dict[str, Any]:
        """刷新后恢复本人的处理状态。"""
        return await platform.proactive.get(who, record_id)

    @router.get('/{record_id}/execution-draft')
    async def execution_draft(record_id: str, who: Caller, version: int = Query(ge=1)) -> dict[str, Any]:
        """只导出本人当前证据草稿，不把提醒转换为执行授权。"""
        return await platform.proactive.execution_draft(who, record_id, version)

    @router.post('/{record_id}/actions')
    async def act(record_id: str, body: ActionInput, who: Caller) -> dict[str, Any]:
        """同一请求重试只产生一次用户决定和业务写入。"""
        return await platform.proactive.act(who, record_id, body)

    return router
