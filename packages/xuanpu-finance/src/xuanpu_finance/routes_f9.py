"""F9运行能力与有界报销摘要的签名查询入口。"""
from __future__ import annotations

from importlib.metadata import version
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store
from xuanpu_finance.routes import Company, Cursor  # noqa: TC001 — FastAPI运行时解析
from xuanpu_finance.routes_f1 import ClaimStatus  # noqa: TC001

router = APIRouter()
SummaryLimit = Annotated[int, Query(ge=1, le=50)]
SummarySearch = Annotated[str, Query(max_length=128)]


@router.get('/v1/finance/capabilities', operation_id='getFinanceCapabilities')
async def capabilities(request: Request, who: FinanceCaller) -> dict[str, Any]:
    """认证后只声明运行边界，不声明个人授权或生产可用。"""
    return {'profile': request.app.state.profile, 'mode': request.app.state.mode,
            'service_version': version('xuanpu-finance'),
            'internal_records_only': request.app.state.profile == 'internal_records'}


@router.get('/v1/finance/expense-claim-summaries', operation_id='listExpenseClaimSummaries')
async def summaries(
    request: Request, who: FinanceCaller, legal_entity_id: Company,
    view: Literal['mine', 'visible'] = 'mine', status: ClaimStatus = None,
    q: SummarySearch = '', cursor: Cursor = None, limit: SummaryLimit = 20,
) -> dict[str, Any]:
    """摘要只含有界元数据，详细事实仍由单据及历史入口返回。"""
    return await get_store(request).list_expense_claim_summaries(
        who, legal_entity_id, view=view, status=status, q=q, cursor=cursor, limit=limit)
