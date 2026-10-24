"""协作动态复用统一通知连接，独立来源游标不覆盖旧提醒的Last-Event-ID。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI响应模型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI依赖
from xuanpu_server.errors import NotFoundError
from xuanpu_server.shared_matters.schemas import SharedChange  # noqa: TC001 —— FastAPI请求模型

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.collaboration.notifications import CollaborationNotifications
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal


@dataclass
class CollaborationCursor:
    """同一SSE连接内的协作来源位置。"""
    after_id: int=0
    visibility_revision: str|None=None


async def collaboration_frames(service: CollaborationNotifications,who: Principal,position: CollaborationCursor) -> AsyncIterator[str]:
    """每帧外发前重新检查，不将列表内容直接当作SSE权限。"""
    page=await service.page(who,after_id=position.after_id)
    items=page['items'];assert isinstance(items,list)
    for item in items:
        assert isinstance(item,dict) and isinstance(item['sequence'],int)
        position.after_id=item['sequence']
        try:
            current=await service.get(who,str(item['id']))
        except NotFoundError:
            continue
        yield f"event: collaboration_notification\ndata: {json.dumps(current,ensure_ascii=False)}\n\n"
    current=await service.page(who,after_id=position.after_id,limit=1)
    revision=current['visibility_revision'];assert isinstance(revision,str)
    if revision!=position.visibility_revision:
        position.visibility_revision=revision
        hint={key:current[key] for key in ('total','unread_total','visibility_revision','permission_revision')}
        yield f"event: collaboration_notifications_changed\ndata: {json.dumps(hint)}\n\n"


def collaboration_notifications_router(platform: Platform) -> APIRouter:
    """补读和标读均由当前员工发起，服务端拒绝其他员工收件箱。"""
    router=APIRouter(prefix='/v1/collaboration-notifications')

    @router.get('')
    async def page(who: Caller,after_id: int=0,before_id: int|None=None,limit: int=100,latest: bool=False) -> dict[str,JsonValue]:
        """独立总数、未读数与来源序号分页。"""
        return await platform.collaboration_notifications.page(who,after_id=after_id,before_id=before_id,limit=limit,latest=latest)

    @router.post('/{identifier}/read')
    async def read(identifier: str,body: SharedChange,who: Caller) -> dict[str,JsonValue]:
        """明确将本人可见动态标读。"""
        return await platform.collaboration_notifications.read(who,identifier,body)

    return router
