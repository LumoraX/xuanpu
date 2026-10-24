"""共享动态补读与标读，由现有统一通知中心展示。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI认证依赖
from xuanpu_server.errors import NotFoundError
from xuanpu_server.shared_matters.schemas import SharedChange  # noqa: TC001 —— 请求模型

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.platform import Platform
    from xuanpu_server.shared_matters.notifications import SharedNotifications
    from xuanpu_server.tenancy import Principal


@dataclass
class SharedNotificationCursor:
    """同一通知连接内的共享游标，不覆盖既有提醒的Last-Event-ID。"""
    after_id: int = 0
    visibility_revision: str | None = None


async def shared_notification_frames(service: SharedNotifications, who: Principal,
                                      position: SharedNotificationCursor) -> AsyncIterator[str]:
    """每帧外发前复查来源，数量提示在全部补读后再查一次当前ACL。"""
    page = await service.page(who, after_id=position.after_id)
    items = page['items']
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict) and isinstance(item['sequence'], int)
        position.after_id = item['sequence']
        try:
            view = await service.get(who, str(item['id']))
        except NotFoundError:
            continue
        yield f"event: shared_notification\ndata: {json.dumps(view, ensure_ascii=False)}\n\n"
    current = await service.page(who, after_id=position.after_id, limit=1)
    revision = current['visibility_revision']
    assert isinstance(revision, str)
    if revision != position.visibility_revision:
        position.visibility_revision = revision
        hint = {key: current[key] for key in ('total', 'unread_total', 'visibility_revision', 'permission_revision')}
        yield f"event: shared_notifications_changed\ndata: {json.dumps(hint)}\n\n"


def shared_notifications_router(platform: Platform) -> APIRouter:
    """接口只含当前来源引用，不复制私有正文或向外部渠道投递。"""
    router = APIRouter(prefix='/v1/shared-notifications')

    @router.get('')
    async def page(who: Caller, after_id: int = 0, before_id: int | None = None,
                   limit: int = 100, latest: bool = False) -> dict[str, JsonValue]:
        """当前ACL过滤后的独立总数、未读数及有界分页。"""
        return await platform.shared_notifications.page(who, after_id=after_id, before_id=before_id, limit=limit, latest=latest)

    @router.post('/{identifier}/read')
    async def read(identifier: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """本人明确标读；同操作号重试保持原结果。"""
        return await platform.shared_notifications.read(who, identifier, body)

    return router
