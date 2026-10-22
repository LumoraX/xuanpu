"""普通报销历史与人工原件核对的独立签名只读入口。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.materials import read_material
from xuanpu_finance.routes import Cursor, Limit  # noqa: TC001 — FastAPI运行时解析

router = APIRouter()


@router.get('/v1/finance/expense-claims/{id}/history', operation_id='listExpenseClaimHistory')
async def history(
    request: Request, id: str, who: FinanceCaller, cursor: Cursor = None, limit: Limit = 50,
) -> dict[str, Any]:
    """返回不可变原快照及实际人工决定，每次重新核对当前单据权限。"""
    return await get_store(request).list_expense_claim_history(who, id, cursor, limit)


@router.get('/v1/finance/expense-claims/{id}/materials/{receipt_id}', operation_id='getExpenseMaterial')
async def material(
    request: Request, id: str, receipt_id: str, who: FinanceCaller,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=262144)] = 131072,
    document_version: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """指定历史版本仍使用当前授权，原件核对不会制造解析或已验真事实。"""
    if who.origin != 'human':
        raise FinanceError('HUMAN_REQUIRED', 403, '仅实际员工可读取原件材料')
    if request.app.state.objects is None:
        raise FinanceError('MATERIAL_STORE_UNAVAILABLE', 503, '独立原件存储未注入，请联系财务管理员')
    return await read_material(get_store(request), request.app.state.objects, who, id,
                               receipt_id, offset, limit, document_version)
