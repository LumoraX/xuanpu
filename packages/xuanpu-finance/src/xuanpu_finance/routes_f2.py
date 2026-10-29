"""原生应付、独立付款记账记录及只读核对的公开API。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store, require_write
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import PayableDraft, PayableUpdate  # noqa: TC001
from xuanpu_finance.routes import (  # noqa: TC001
    Company,
    Cursor,
    IdempotencyKey,
    Limit,
    VersionRequest,
)
from xuanpu_finance.routes_f1 import ClaimStatus  # noqa: TC001

router = APIRouter()
DocumentId = Annotated[str | None, Query(min_length=1, max_length=128)]
DocumentKind = Annotated[Literal["expense_claim", "payable", "funding_case"], Query()]
BusinessKind = Annotated[Literal["supplier", "contract", "order", "acceptance"], Query()]
Search = Annotated[str | None, Query(max_length=128)]


@router.post("/v1/finance/payables", status_code=201, operation_id="createPayable")
async def create_payable(
    request: Request, body: PayableDraft, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """只创建本人应付草稿，不自动审批或产生外部请求。"""
    require_write(request)
    return await get_store(request).create_payable(who, body, key)


@router.get("/v1/finance/payables", operation_id="listPayables")
async def list_payables(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    cursor: Cursor = None,
    limit: Limit = 50,
    status: ClaimStatus = None,
) -> dict[str, Any]:
    """恢复当前员工有权的原生应付和独立筛选总数。"""
    return await get_store(request).list_payables(
        who, legal_entity_id, cursor=cursor, limit=limit, status=status
    )


@router.get("/v1/finance/payables/{id}", operation_id="getPayable")
async def get_payable(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """读取应付内容版本与持久审批，不通过报销接口混读。"""
    return await get_store(request).get_payable(who, id)


@router.put("/v1/finance/payables/{id}", operation_id="updatePayable")
async def update_payable(
    request: Request, id: str, body: PayableUpdate, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """完整修订可编辑版本，不能改所属公司或结果状态。"""
    require_write(request)
    return await get_store(request).update_payable(who, id, body, key)


@router.get("/v1/finance/payments", operation_id="listPayments")
async def list_payments(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    document_id: DocumentId = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """查询实际付款记录，pending与unknown均不表示已付。"""
    return await get_store(request).list_payments(
        who, legal_entity_id, document_id=document_id, cursor=cursor, limit=limit
    )


@router.get("/v1/finance/payments/{id}", operation_id="getPayment")
async def get_payment(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """读取受权业务的付款及渠道回单证据。"""
    return await get_store(request).get_payment(who, id)


@router.get("/v1/finance/postings", operation_id="listPostings")
async def list_postings(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    document_id: DocumentId = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """记账维度与付款独立，凭证回执才能证明已入账。"""
    return await get_store(request).list_postings(
        who, legal_entity_id, document_id=document_id, cursor=cursor, limit=limit
    )


@router.get("/v1/finance/postings/{id}", operation_id="getPosting")
async def get_posting(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """返回凭证和冲回事实，不覆盖原始已入账回执。"""
    return await get_store(request).get_posting(who, id)


@router.get("/v1/finance/action-plans", operation_id="listActionPlans")
async def list_plans(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    target_id: DocumentId = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """恢复可见行动计划与专用工作流，不依赖聊天记忆。"""
    return await get_store(request).list_action_plans(
        who, legal_entity_id, target_id=target_id, cursor=cursor, limit=limit
    )


@router.get("/v1/finance/settlement-options", operation_id="getSettlementOptions")
async def settlement_options(
    request: Request, who: FinanceCaller, legal_entity_id: Company
) -> dict[str, Any]:
    """只显示当前员工经办资格和公司配置的开放期间。"""
    return await get_store(request).get_settlement_options(who, legal_entity_id)


@router.get("/v1/finance/catalog/business-documents", operation_id="listBusinessDocuments")
async def business_documents(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    kind: BusinessKind,
    query: Search = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """目录返回已取得的固定版本，不把聊天自报材料当真实快照。"""
    return await get_store(request).list_business_documents(
        who, legal_entity_id, kind=kind, query=query, cursor=cursor, limit=limit
    )


@router.get("/v1/finance/catalog/payee-accounts", operation_id="listPayeeAccounts")
async def payee_accounts(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    document_kind: DocumentKind,
    document_id: Annotated[str, Query(min_length=1, max_length=128)],
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """仅显示该业务允许的已核实账户脱敏版本。"""
    return await get_store(request).list_payee_accounts(
        who,
        legal_entity_id,
        document_kind=document_kind,
        document_id=document_id,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/v1/finance/operations/{id}/reconcile", status_code=202, operation_id="reconcileOperation"
)
async def reconcile(
    request: Request, id: str, body: VersionRequest, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """人工请求只读查单，不能直接指定已付或已入账。"""
    if who.origin != "human":
        raise FinanceError("HUMAN_REQUIRED", 403, "请本人明确发起核对")
    return await get_store(request).reconcile_operation(who, id, body.expected_version, key)
