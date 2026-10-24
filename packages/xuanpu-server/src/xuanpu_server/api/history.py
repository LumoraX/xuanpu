"""按会话归属授权的对话历史；不开放租户审计或工具原始输入输出。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析路由标注
from xuanpu_server.controlplane.models import AuditQuery

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.platform import Platform

_FIELDS = {
    "message_sent": ("text",),
    "turn_text": ("text",),
    "turn_started": (),
    "turn_suspended": (),
    "turn_completed": (),
    "turn_failed": (),
    "tool_call_completed": ("name", "is_error", "duration_ms"),
}


class HistoryQuery(BaseModel):
    """仅允许分页参数，拒绝外部指定租户、员工和审计过滤条件。"""

    model_config = ConfigDict(extra="forbid")
    after_id: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


def history_view(record: AuditRecord) -> dict[str, Any]:
    """只投影显示历史所需的字段；不返回错误详情、工具输入输出及内部元数据。"""
    return {
        "audit_id": record.audit_id,
        "at": record.at,
        "kind": record.kind,
        "data": {key: record.data[key] for key in _FIELDS[record.kind] if key in record.data},
    }


def history_router(platform: Platform) -> APIRouter:
    """使用既有会话归属校验的历史路由。"""
    router = APIRouter()

    @router.get("/v1/sessions/{session_id}/history")
    async def history(
        session_id: str, who: Caller, query: Annotated[HistoryQuery, Query()]
    ) -> dict[str, Any]:
        """按原始记录页推进游标：被过滤的空页仍可继续，单次查询成本有界。"""
        session = await platform.sessions.content(who, session_id)
        records = await platform.store.query_audit(
            session.tenant_id,
            AuditQuery(session_id=session.session_id, after_id=query.after_id, limit=query.limit),
        )
        await platform.sessions.content(who, session_id)
        return {
            "records": [history_view(record) for record in records if record.kind in _FIELDS],
            "next_after_id": records[-1].audit_id if len(records) == query.limit else None,
        }

    return router
