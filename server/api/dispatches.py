"""派工细节只读接口（R7-3）：小院「经过」里的任务说明、验收、结果摘录与问答。

只给本人：需要当前启用的员工身份，机器身份 403，他人的与不存在的一律 404。匠人的内部过程
（推理、工具参数与输出）不在这里；结果只给唤醒时登记的匠人回复摘录。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from server.engine.tracing.models import TraceOwner

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.engine.butler.models import DispatchRecord
    from server.engine.butler.wake_models import ButlerEvent, PendingItem
    from server.platform import Platform

RESULT_MAX = 600
CHECKS = ("terminal", "succeeded", "outputs", "no_pending", "no_unknown")


def dispatch_detail(record: DispatchRecord, events: tuple[ButlerEvent, ...],
                    items: tuple[PendingItem, ...]) -> dict[str, Any]:
    """一次派工给员工看的细节；不回显租户与员工标识。"""
    decided = [e.decision for e in events if e.decision is not None]
    checked = [d for d in decided if d.checks]
    results = [d.artisan_text for d in decided if d.artisan_text]
    return {
        "dispatch_id": record.dispatch_id, "trace_id": record.trace_id,
        "group_id": record.group_id,
        "artisan": {"package": record.artisan.package, "entry": record.artisan.entry},
        "host": record.host, "state": record.state, "end_reason": record.end_reason,
        "instruction": record.instruction,
        "created_at": record.created_at.isoformat(), "updated_at": record.updated_at.isoformat(),
        # 最近一次带验收的决定；还没验收过为 null，不用全假冒充
        "checks": {key: bool(checked[-1].checks.get(key)) for key in CHECKS} if checked else None,
        "result": results[-1][:RESULT_MAX] if results else None,
        "items": [_item(item) for item in items],
    }


def _item(item: PendingItem) -> dict[str, Any]:
    """问题、审批、裁决或代答记录；代答带出处原文。"""
    return {
        "pending_id": item.pending_id, "kind": item.kind, "question": item.question,
        "state": item.state,
        "answer": item.answer,
        "source_quote": None if item.answer_source is None else item.answer_source.quote,
        "created_at": item.created_at.isoformat(),
    }


def dispatches_router(platform: Platform) -> APIRouter:
    """注册派工细节接口。"""
    router = APIRouter()

    async def owner(who: Principal) -> TraceOwner:
        """只有当前启用的员工能看自己的派工。"""
        tenant_id, user_id = await platform.workspace.require_employee(who)
        return TraceOwner(tenant_id=tenant_id, user_id=user_id)

    @router.get("/v1/assistant/dispatches/{dispatch_id}")
    async def detail(dispatch_id: str, who: Caller) -> dict[str, Any]:
        """一次派工的细节；他人的与不存在的 404。"""
        me = await owner(who)
        record = await platform.dispatches.get(me, dispatch_id)
        wake = platform.store.wake_store
        events = await wake.events_for_dispatch(me, dispatch_id)
        items = await wake.list_pending(me, open_only=False, dispatch_id=dispatch_id)
        return dispatch_detail(record, events, items)

    return router
