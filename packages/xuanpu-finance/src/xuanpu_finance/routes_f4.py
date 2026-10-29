"""原生财务提醒与运营概览，不能由通知触发审批或外部写入。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store, require_write
from xuanpu_finance.routes import (  # noqa: TC001
    Company,
    Cursor,
    IdempotencyKey,
    Limit,
    VersionRequest,
)

router = APIRouter()


@router.get('/v1/finance/notifications', operation_id='listFinanceNotifications')
async def notifications(request: Request, who: FinanceCaller, legal_entity_id: Company,
                        cursor: Cursor = None, limit: Limit = 50, unread_only: bool = False) -> dict[str, Any]:
    """当前来源权限一致的站内提醒，未读数独立于当前页。"""
    return await get_store(request).list_finance_notifications(who, legal_entity_id, cursor=cursor, limit=limit, unread_only=unread_only)


@router.post('/v1/finance/notifications/{id}/read', operation_id='readFinanceNotification')
async def read_notification(request: Request, id: str, body: VersionRequest,
                            key: IdempotencyKey, who: FinanceCaller) -> dict[str, Any]:
    """仅本人明确标记已读；不会产生审批、材料收到或渠道成功事实。"""
    require_write(request)
    return await get_store(request).read_finance_notification(who, id, body.expected_version, key)


@router.get('/v1/finance/overview', operation_id='getFinanceOverview')
async def overview(request: Request, who: FinanceCaller, legal_entity_id: Company) -> dict[str, Any]:
    """仅当前reviewer读取同公司指标；进程探活与业务结果分开。"""
    data = await get_store(request).get_finance_overview(who, legal_entity_id)
    runtime = {'mode': request.app.state.mode, 'instance_lock_lost': request.app.state.instance_lock_lost}
    for name in ('worker', 'settlement', 'compliance', 'notification'):
        error_name = 'worker_error' if name == 'worker' else name+'_worker_error'
        task_name = 'worker_task' if name == 'worker' else name+'_task'
        task = getattr(request.app.state, task_name, None)
        runtime[name] = 'failed' if getattr(request.app.state, error_name, None) is not None else 'running' if task is not None and not task.done() else 'not_started'
    return {**data, 'runtime': runtime}
