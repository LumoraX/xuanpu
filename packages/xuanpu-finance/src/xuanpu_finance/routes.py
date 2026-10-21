"""财务 F0 路由：全部复用服务端身份、公司权限与持久存储。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from xuanpu_finance.dependencies import FinanceCaller, get_store, require_write
from xuanpu_finance.models import InvoiceImport  # noqa: TC001 — FastAPI 运行时解析

router = APIRouter()
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Company = Annotated[str, Query(min_length=1, max_length=128)]
Cursor = Annotated[str | None, Query(max_length=2048)]
InvoiceNumber = Annotated[str | None, Query(max_length=64)]


class VersionRequest(BaseModel):
    """取消也要绑定当前业务版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: Annotated[int, Field(ge=1)]


@router.get("/v1/finance/legal-entities", operation_id="listLegalEntities")
async def companies(
    request: Request, who: FinanceCaller, cursor: Cursor = None, limit: Limit = 50
) -> dict[str, Any]:
    """只列真实有权处理的公司。"""
    return await get_store(request).list_companies(who, cursor=cursor, limit=limit)


@router.get("/v1/finance/integrations", operation_id="listIntegrations")
async def integrations(
    request: Request, who: FinanceCaller, legal_entity_id: Company
) -> dict[str, Any]:
    """明确展示未配置通道，不生成虚假能力。"""
    return await get_store(request).list_integrations(who, legal_entity_id)


@router.post("/v1/finance/invoice-imports", status_code=202, operation_id="importInvoices")
async def imports(
    request: Request, body: InvoiceImport, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """数据库受理完成后返回操作 ID，补件关联原持久异常。"""
    require_write(request)
    return await get_store(request).accept_import(
        who,
        body.legal_entity_id,
        body.files,
        key,
        source_exception_id=body.source_exception_id,
        expected_exception_version=body.expected_exception_version,
    )


@router.get("/v1/finance/operations/{id}", operation_id="getOperation")
async def operation(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """读取持久操作及每项真实结果。"""
    return await get_store(request).get_operation(who, id)


@router.post("/v1/finance/operations/{id}/cancel", status_code=202, operation_id="cancelOperation")
async def cancel(
    request: Request, id: str, body: VersionRequest, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """取消尚未处理项，保留已完成的原件和票据。"""
    require_write(request, cancel=True)
    return await get_store(request).cancel_operation(who, id, body.expected_version, key)


@router.get("/v1/finance/invoices", operation_id="listInvoices")
async def invoices(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    cursor: Cursor = None,
    limit: Limit = 50,
    invoice_number: InvoiceNumber = None,
) -> dict[str, Any]:
    """返回权限过滤后的独立总数与稳定分页。"""
    return await get_store(request).list_invoices(
        who, legal_entity_id, cursor=cursor, limit=limit, number=invoice_number
    )


@router.get("/v1/finance/invoices/{id}", operation_id="getInvoice")
async def invoice(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """读取本地票面状态，不推断税务有效性。"""
    return await get_store(request).get_invoice(who, id)


@router.get("/v1/finance/invoices/{id}/lines", operation_id="getInvoiceLines")
async def lines(
    request: Request, id: str, who: FinanceCaller, cursor: Cursor = None, limit: Limit = 50
) -> dict[str, Any]:
    """限量读取逐行税率和金额。"""
    return await get_store(request).list_invoice_lines(who, id, cursor=cursor, limit=limit)


@router.get("/v1/finance/invoices/{id}/fields", operation_id="getInvoiceFields")
async def fields(
    request: Request, id: str, who: FinanceCaller, cursor: Cursor = None, limit: Limit = 50
) -> dict[str, Any]:
    """限量读取原始字段和来源路径。"""
    return await get_store(request).list_invoice_fields(who, id, cursor=cursor, limit=limit)


@router.get("/v1/finance/invoices/{id}/evidence", operation_id="getInvoiceEvidence")
async def evidence(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """只提供脱敏证据引用，原件存储凭据不进入聊天。"""
    return await get_store(request).get_invoice_evidence(who, id)


@router.get("/v1/finance/exceptions", operation_id="listExceptions")
async def exceptions(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """查询重启后仍保留的补件和异常待办。"""
    return await get_store(request).list_exceptions(
        who, legal_entity_id, cursor=cursor, limit=limit
    )
