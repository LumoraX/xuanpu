"""原生提醒和通知 HTTP/SSE 接口；权限沿用 Caller 和登录 CSRF。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from xuanpu_server.api.auth import ACT_AS_HEADER, Caller, caller  # noqa: TC001
from xuanpu_server.errors import InvalidRequestError, PlatformError
from xuanpu_server.reminders.models import ReminderInput

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.platform import Platform
    from xuanpu_server.reminders.models import NotificationRecord, ReminderRecord
    from xuanpu_server.reminders.service import ReminderService
    from xuanpu_server.tenancy import Principal


class CreateReminderBody(ReminderInput):
    """创建额外带客户端幂等请求标识。"""

    request_id: str = Field(min_length=1, max_length=128)


class UpdateReminderBody(ReminderInput):
    """编辑带客户端读到的版本。"""

    version: int = Field(ge=1)


def reminder_view(record: ReminderRecord) -> dict[str, Any]:
    """创建原指纹只供存储，不对外暴露。"""
    result = asdict(record)
    del result["original_fingerprint"]
    return result


async def notification_view(
    service: ReminderService, who: Principal, record: NotificationRecord
) -> dict[str, Any]:
    """向用户展示外部发送状态，不暴露租约令牌。"""
    deliveries = await service.deliveries(who, record.notification_id)
    return {
        **asdict(record),
        "attention_active": await service.store.notification_current(record),
        "deliveries": [
            {
                "channel": d.channel,
                "status": d.status,
                "attempts": d.attempts,
                "last_error_code": d.error_code,
            }
            for d in deliveries
        ],
    }


async def notification_events(
    request: Request, service: ReminderService, who: Principal, after_id: int
) -> AsyncIterator[str]:
    """持久表补读，逐轮重新认证 cookie/API key 并给出心跳。"""
    cursor = after_id
    while not await request.is_disconnected():
        try:
            current = await caller(
                request, request.headers.get("authorization"), request.headers.get(ACT_AS_HEADER)
            )
            if (current.tenant_id, current.user_id) != (who.tenant_id, who.user_id):
                return
            found = await service.notifications(current, cursor)
            for record in found:
                view = await notification_view(service, current, record)
                cursor = record.notification_id
                yield f"id: {cursor}\nevent: notification\ndata: {json.dumps(view, ensure_ascii=False)}\n\n"
        except PlatformError:
            return
        yield ": heartbeat\n\n"
        # 满页立即补读，空页每两秒重新认证；断连和关闭均可取消。
        if len(found) < 100:
            await asyncio.sleep(2)


def reminders_router(platform: Platform) -> APIRouter:
    """注册本人提醒、通知及事件路由。"""
    router = APIRouter()
    service = platform.reminders

    @router.get("/v1/reminders/channels")
    async def channels(who: Caller) -> dict[str, list[str]]:
        """查询本人可选渠道。"""
        return {"channels": await service.channels(who)}

    @router.post("/v1/reminders")
    async def create(body: CreateReminderBody, who: Caller) -> dict[str, Any]:
        """明确确认后落库，创建成功才返回记录。"""
        payload = ReminderInput.model_validate(body.model_dump(exclude={"request_id"}))
        return reminder_view(await service.create(who, payload, body.request_id))

    @router.get("/v1/reminders")
    async def listing(
        who: Caller, status: Literal["active", "completed", "cancelled"] | None = None
    ) -> dict[str, Any]:
        """列出本人的最近一百条提醒。"""
        return {
            "items": [reminder_view(record) for record in await service.list(who, status=status)]
        }

    @router.put("/v1/reminders/{reminder_id}")
    async def update(reminder_id: str, body: UpdateReminderBody, who: Caller) -> dict[str, Any]:
        """编辑本人活动提醒，版本过期返回409。"""
        payload = ReminderInput.model_validate(body.model_dump(exclude={"version"}))
        return reminder_view(await service.update(who, reminder_id, payload, body.version))

    @router.delete("/v1/reminders/{reminder_id}")
    async def cancel(
        reminder_id: str, who: Caller, version: Annotated[int, Query(ge=1)]
    ) -> dict[str, Any]:
        """取消本人活动提醒。"""
        return reminder_view(await service.cancel(who, reminder_id, version))

    @router.get("/v1/notifications")
    async def notifications(
        who: Caller,
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        latest: bool = False,
    ) -> dict[str, Any]:
        """按游标读取通知；latest用于最近窗口初载。"""
        found = await service.notifications(who, after_id, limit, latest=latest)
        return {"items": [await notification_view(service, who, record) for record in found]}

    @router.post("/v1/notifications/{notification_id}/read")
    async def read(notification_id: int, who: Caller) -> dict[str, Any]:
        """幂等标记本人的通知已读。"""
        return await notification_view(service, who, await service.read(who, notification_id))

    @router.get("/v1/notifications/events")
    async def events(
        request: Request,
        who: Caller,
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    ) -> StreamingResponse:
        """EventSource重连使用 Last-Event-ID，超出范围和非法游标拒绝。"""
        await service.require_employee(who)
        last = request.headers.get("last-event-id")
        if last is not None:
            try:
                after_id = max(after_id, int(last))
            except ValueError as exc:
                raise InvalidRequestError("通知游标无效") from exc
            if int(last) < 0:
                raise InvalidRequestError("通知游标无效")
        if after_id > 9223372036854775807:
            raise InvalidRequestError("通知游标无效")
        return StreamingResponse(
            notification_events(request, service, who, after_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
