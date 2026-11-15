"""执行追踪（R5-B）的只读接口：本人的 trace 列表、执行树与瀑布。

只返回元数据；正文从会话历史按原权限读取。不存在与不属于本人一律 404。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from xuanpu_server.api.auth import (  # noqa: TC001 —— FastAPI 运行时解析依赖
    ACT_AS_HEADER,
    Caller,
    caller,
)
from xuanpu_server.api.trace_events import trace_frames
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.tracing.models import TraceOwner
from xuanpu_server.tracing.store import PAGE_DEFAULT, PAGE_MAX
from xuanpu_server.tracing.views import trace_view, tree_view
from xuanpu_server.tracing.waterfall import waterfall

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal


def traces_router(platform: Platform) -> APIRouter:
    """注册本人执行追踪的只读路由。"""
    router = APIRouter()
    store = platform.store.trace_store

    async def owner(who: Principal) -> TraceOwner:
        """只有当前启用的员工能看自己的执行记录。"""
        tenant_id, user_id = await platform.workspace.require_employee(who)
        return TraceOwner(tenant_id=tenant_id, user_id=user_id)

    @router.get("/v1/traces")
    async def listing(
        who: Caller,
        active: bool | None = None,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    ) -> dict[str, Any]:
        """按创建时间倒序分页；总数与当前页无关。"""
        page = await store.list(await owner(who), active=active, cursor=cursor, limit=limit)
        return {"items": [trace_view(item) for item in page.items],
                "next_cursor": page.next_cursor, "total": page.total}

    @router.get("/v1/traces/{trace_id}")
    async def detail(trace_id: str, who: Caller) -> dict[str, Any]:
        """一条 trace 的执行树。"""
        return tree_view(await store.get(await owner(who), trace_id))

    @router.get("/v1/traces/{trace_id}/waterfall")
    async def rows(trace_id: str, who: Caller) -> dict[str, Any]:
        """按开始时间排列的瀑布行；运行中的行没有时长。"""
        view = await store.get(await owner(who), trace_id)
        return {**trace_view(view.trace), "rows": [asdict(row) for row in waterfall(view)]}

    @router.get("/v1/traces/{trace_id}/events")
    async def events(
        trace_id: str, request: Request, who: Caller,
        after_seq: Annotated[int, Query(ge=0, le=2**31)] = 0,
    ) -> StreamingResponse:
        """SSE 增量；``Last-Event-ID`` 优先于 ``after_seq``。不存在或无权在建流前 404。"""
        me = await owner(who)
        await store.get(me, trace_id)
        last = request.headers.get("last-event-id")
        if last is not None:
            if not last.isdigit() or int(last) > 2**31:
                raise InvalidRequestError("事件游标无效")
            after_seq = int(last)

        async def reauth() -> TraceOwner:
            """按原请求凭据重新认证并重查员工状态。"""
            current = await caller(request, request.headers.get("authorization"),
                                   request.headers.get(ACT_AS_HEADER))
            return await owner(current)

        return StreamingResponse(
            trace_frames(store, me, trace_id, after_seq=after_seq, reauth=reauth,
                         disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
