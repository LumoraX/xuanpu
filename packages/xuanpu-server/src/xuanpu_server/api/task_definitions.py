"""本人执行安排接口，复用认证、CSRF和员工内容边界。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query

from xuanpu_server.api.auth import Caller  # noqa: TC001
from xuanpu_server.task_triggers.models import (  # noqa: TC001
    DefinitionAction,
    DefinitionCreate,
    DefinitionUpdate,
)
from xuanpu_server.task_triggers.store import public

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def task_definitions_router(platform: Platform) -> APIRouter:
    """接入层不持有调度，不直接调用模型。"""
    router=APIRouter(prefix='/v1/task-definitions')
    service=platform.task_definitions

    @router.post('',status_code=202)
    async def create(body: DefinitionCreate,who: Caller) -> dict[str,Any]:
        """确认创建后返回已冻结范围。"""
        return public(await service.create(who,body))

    @router.get('')
    async def listing(who: Caller,after_id: str | None=None,limit: Annotated[int,Query(ge=1,le=100)]=100) -> dict[str,Any]:
        """本人稳定分页。"""
        return {'items':[public(d) for d in await service.list(who,after_id=after_id,limit=limit)]}

    @router.get('/{definition_id}')
    async def get(definition_id: str,who: Caller) -> dict[str,Any]:
        """跨员工标识不会泄漏存在性。"""
        return public(await service.get(who,definition_id))

    @router.put('/{definition_id}')
    async def update(definition_id: str,body: DefinitionUpdate,who: Caller) -> dict[str,Any]:
        """修改必须重新确认并匹配当前state_version。"""
        return public(await service.update(who,definition_id,body))

    @router.post('/{definition_id}/pause')
    async def pause(definition_id: str,body: DefinitionAction,who: Caller) -> dict[str,Any]:
        """默认不停止已在途的实例。"""
        return public(await service.act(who,definition_id,'pause',body))

    @router.post('/{definition_id}/resume')
    async def resume(definition_id: str,body: DefinitionAction,who: Caller) -> dict[str,Any]:
        """按原错过策略恢复，不无限补跑。"""
        return public(await service.act(who,definition_id,'resume',body))

    @router.post('/{definition_id}/cancel')
    async def cancel(definition_id: str,body: DefinitionAction,who: Caller) -> dict[str,Any]:
        """撤销未开始授权并对在途提出停止。"""
        return public(await service.act(who,definition_id,'cancel',body))

    @router.get('/{definition_id}/occurrences')
    async def occurrences(definition_id: str,who: Caller,after_id: str | None=None,
                          limit: Annotated[int,Query(ge=1,le=100)]=100) -> dict[str,Any]:
        """每次发生及关联任务独立补读。"""
        return {'items':[o.model_dump(mode='json') for o in await service.occurrences(who,definition_id,after_id=after_id,limit=limit)]}

    return router
