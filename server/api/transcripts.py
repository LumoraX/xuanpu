"""跨节点 Transcript 查询与内部节点间热读代理路由（ADR 0026）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Header, Query

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from server.core.errors import AuthenticationError
from server.engine.transcripts.models import TranscriptMode  # noqa: TC001 —— FastAPI 运行时解析
from server.engine.transcripts.reader import TICKET_HEADER

if TYPE_CHECKING:
    from server.engine.transcripts.models import TranscriptIndexRecord
    from server.platform import Platform


def _index_view(record: TranscriptIndexRecord) -> dict[str, Any]:
    """对外元数据视图：隐藏本机物理相对路径与内部地址。"""
    return {
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "parent_span_id": record.parent_span_id,
        "kind": record.kind,
        "node_id": record.node_id,
        "status": record.status,
        "error_code": record.error_code,
        "line_count": record.line_count,
        "byte_size": record.byte_size,
        "archived": record.archived_at is not None,
        "created_at": record.created_at.isoformat(),
    }


def transcripts_router(platform: Platform) -> APIRouter:
    """挂载 Transcript 查询（用户/管理员）与内部跨节点签名只读热代理接口。"""
    router = APIRouter()

    @router.get("/v1/traces/{trace_id}/transcripts")
    async def list_trace_transcripts(trace_id: str, who: Caller) -> dict[str, Any]:
        """列出一条 trace 下全部跨节点 Transcript 元数据（普通员工只看本人，管理员可查本租户）。"""
        tenant = who.require_tenant()
        scoped_user = None if who.role == "tenant_admin" else who.user_id
        items = await platform.transcripts.list_for_trace(tenant, trace_id, user_id=scoped_user)
        return {"trace_id": trace_id, "items": [_index_view(r) for r in items]}

    @router.get("/v1/traces/{trace_id}/transcripts/{span_id}")
    async def read_transcript_slice(
        trace_id: str,
        span_id: str,
        who: Caller,
        mode: TranscriptMode = Query(default="compact"),
        start_line: int = Query(default=1, ge=1),
        end_line: int = Query(default=200, ge=1, le=50000),
    ) -> dict[str, Any]:
        """三态透明切片读取单 span 的 Transcript（本节点直读 → 远端热读 → 对象存储冷归档）。"""
        tenant = who.require_tenant()
        scoped_user = None if who.role == "tenant_admin" else who.user_id
        sliced = await platform.transcripts.read(
            tenant, span_id, user_id=scoped_user, mode=mode,
            start_line=start_line, end_line=end_line,
        )
        if sliced.trace_id != trace_id:
            from server.core.errors import NotFoundError

            raise NotFoundError("Transcript 索引不存在")
        return sliced.model_dump(mode="json")

    @router.get("/v1/diagnostics/failed-transcripts")
    async def list_failed_transcripts(
        who: Caller, limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """近期异常 Transcript 列表（供开发者排障与未来玄圃自我排查只读调用）。"""
        tenant = who.require_tenant()
        scoped_user = None if who.role == "tenant_admin" else who.user_id
        items = await platform.transcripts.list_failed_traces(
            tenant, user_id=scoped_user, limit=limit
        )
        return {"items": [_index_view(r) for r in items]}

    @router.get("/internal/nodes/local/transcripts/{span_id}")
    async def internal_local_transcript(
        span_id: str,
        tenant_id: str = Query(min_length=1, max_length=128),
        mode: TranscriptMode = Query(default="compact"),
        start_line: int = Query(default=1, ge=1),
        end_line: int = Query(default=200, ge=1, le=50000),
        ticket: str | None = Header(default=None, alias=TICKET_HEADER),
    ) -> dict[str, Any]:
        """集群内部跨节点热读只读接口：强制校验短期 HMAC 票据，仅读本节点热盘。"""
        if not ticket:
            raise AuthenticationError("缺少节点间访问票据")
        platform.transcript_signer.verify(
            ticket, tenant_id=tenant_id, node_id=platform.instance_id, span_id=span_id
        )
        sliced = await platform.transcripts.read_local_only(
            tenant_id, span_id, mode=mode, start_line=start_line, end_line=end_line
        )
        return sliced.model_dump(mode="json")

    return router
