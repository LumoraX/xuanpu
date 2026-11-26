"""执行追踪（R5-B）的只读接口：本人的 trace 列表、执行树与瀑布。

只返回元数据；正文从会话历史按原权限读取。不存在与不属于本人一律 404。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from server.api.auth import (  # noqa: TC001 —— FastAPI 运行时解析依赖
    ACT_AS_HEADER,
    Caller,
    caller,
)
from server.api.trace_events import trace_frames
from server.core.controlplane.models import AuditQuery
from server.core.errors import InvalidRequestError, NotFoundError
from server.engine.tracing.models import TraceOwner
from server.engine.tracing.store import PAGE_DEFAULT, PAGE_MAX
from server.engine.tracing.views import trace_view, tree_view
from server.engine.tracing.waterfall import waterfall

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.engine.tracing.models import SpanRecord
    from server.platform import Platform

_AUDIT_OUTPUT_LIMIT = 4000


async def _lookup_tool_audit(
    platform: Platform, me: TraceOwner, span: SpanRecord
) -> dict[str, Any]:
    """按本人归属与时间窗口从审计日志反查单个工具节点的入参与输出。"""
    call_id = span.span_id.removeprefix("tl.") if span.kind == "tool" and span.span_id.startswith("tl.") else None
    arguments: str | None = None
    output: str | None = None
    is_error: bool | None = None
    duration_ms: int | None = None
    if call_id is not None:
        since = span.started_at.timestamp() - 5.0
        end_ts = span.ended_at.timestamp() if span.ended_at is not None else span.started_at.timestamp() + 300.0
        for event_kind in ("tool_call_started", "tool_call_completed", "web_search_completed"):
            records = await platform.store.query_audit(
                me.tenant_id,
                AuditQuery(user_id=me.user_id, kind=event_kind, since=since, until=end_ts + 5.0, limit=200),
            )
            for rec in records:
                if rec.data.get("call_id") != call_id:
                    continue
                if event_kind == "tool_call_started" and arguments is None:
                    raw_args = rec.data.get("arguments")
                    arguments = raw_args if isinstance(raw_args, str) else None
                elif event_kind == "tool_call_completed":
                    raw_out = rec.data.get("output")
                    if isinstance(raw_out, str):
                        output = raw_out[:_AUDIT_OUTPUT_LIMIT] + (
                            "…（已截断）" if len(raw_out) > _AUDIT_OUTPUT_LIMIT else ""
                        )
                    if isinstance(rec.data.get("is_error"), bool):
                        is_error = rec.data["is_error"]
                    if isinstance(rec.data.get("duration_ms"), int):
                        duration_ms = rec.data["duration_ms"]
                elif event_kind == "web_search_completed":
                    query = rec.data.get("query")
                    if arguments is None and isinstance(query, str) and query:
                        arguments = query
                    if output is None and isinstance(rec.data.get("sources"), list):
                        urls = [
                            str(src.get("url"))
                            for src in rec.data["sources"]
                            if isinstance(src, dict) and src.get("url")
                        ]
                        if urls:
                            output = "\n".join(urls)[:_AUDIT_OUTPUT_LIMIT]
                    if is_error is None:
                        is_error = rec.data.get("status") == "failed"
    return {
        "call_id": call_id,
        "arguments": arguments,
        "output": output,
        "is_error": is_error,
        "duration_ms": duration_ms,
    }


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

    @router.get("/v1/traces/{trace_id}/spans/{span_id}/audit")
    async def span_audit(trace_id: str, span_id: str, who: Caller) -> dict[str, Any]:
        """按本人 trace 归属反查单个工具 span 对应的审计入参与返回值（不改变 trace 存储脱敏）。"""
        me = await owner(who)
        view = await store.get(me, trace_id)
        span = next((s for s in view.spans if s.span_id == span_id), None)
        if span is None:
            raise NotFoundError("执行节点不存在")
        audit = await _lookup_tool_audit(platform, me, span)
        return {
            "trace_id": trace_id,
            "span_id": span.span_id,
            "kind": span.kind,
            "name": span.name,
            "state": span.state,
            "say": span.say,
            **audit,
        }

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
