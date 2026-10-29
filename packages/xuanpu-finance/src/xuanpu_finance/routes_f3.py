"""发票查验、红字和独立档案审计的受限API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from xuanpu_finance.compliance.models import (  # noqa: TC001 - FastAPI运行时解析
    RedLetterInvoiceLink,
    TaxAdjustmentEvidence,
)
from xuanpu_finance.dependencies import FinanceCaller, get_store, require_write
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.routes import (  # noqa: TC001
    Company,
    Cursor,
    IdempotencyKey,
    Limit,
    VersionRequest,
)

router = APIRouter()


@router.post("/v1/finance/invoices/{id}/verifications", status_code=202, operation_id="verifyInvoice")
async def verify_invoice(request: Request, id: str, body: VersionRequest,
                         key: IdempotencyKey, who: FinanceCaller) -> dict[str, Any]:
    """受理真实外查，预期版本为合规状态版本。"""
    require_write(request)
    return await get_store(request).verify_invoice(who, id, body.expected_version, key)


@router.get("/v1/finance/invoices/{id}/verifications", operation_id="listInvoiceVerifications")
async def verifications(request: Request, id: str, who: FinanceCaller,
                        cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """读取当前有权票据的实际查验历史。"""
    return await get_store(request).list_invoice_verifications(who, id, cursor=cursor, limit=limit)


@router.get("/v1/finance/invoices/{id}/compliance", operation_id="getInvoiceCompliance")
async def compliance(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """返回独立合规版本、税务和红冲余额投影。"""
    return await get_store(request).get_invoice_compliance(who, id)


@router.get("/v1/finance/compliance-options", operation_id="getComplianceOptions")
async def options(request: Request, who: FinanceCaller, legal_entity_id: Company) -> dict[str, Any]:
    """只展示当前受控政策及经办资格，不返回凭据。"""
    return await get_store(request).get_compliance_options(who, legal_entity_id)


@router.get("/v1/finance/compliance-action-plans", operation_id="listCompliancePlans")
async def plans(request: Request, who: FinanceCaller, legal_entity_id: Company,
                cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """恢复独立目标的税务与归档原计划。"""
    return await get_store(request).list_compliance_plans(who, legal_entity_id, cursor=cursor, limit=limit)


@router.get("/v1/finance/tax-confirmations", operation_id="listTaxConfirmations")
async def confirmations(request: Request, who: FinanceCaller, legal_entity_id: Company,
                        cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """实际税务确认与ERP记账分别保留。"""
    return await get_store(request).list_tax_confirmations(who, legal_entity_id, cursor=cursor, limit=limit)


@router.get("/v1/finance/red-letter-cases", operation_id="listRedLetterCases")
async def cases(request: Request, who: FinanceCaller, legal_entity_id: Company,
                cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """展示从受控权威快照取得的确认单。"""
    return await get_store(request).list_red_letter_cases(who, legal_entity_id, cursor=cursor, limit=limit)


@router.get("/v1/finance/red-letter-cases/{id}", operation_id="getRedLetterCase")
async def case(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """本地超时只表示待核对，不伪造权威作废。"""
    return await get_store(request).get_red_letter_case(who, id)


@router.post("/v1/finance/red-letter-cases/{id}/refresh", status_code=202, operation_id="refreshRedLetterCase")
async def refresh(request: Request, id: str, body: VersionRequest,
                  key: IdempotencyKey, who: FinanceCaller) -> dict[str, Any]:
    """主动查询实际确认单，不发起新确认或开票。"""
    require_write(request)
    return await get_store(request).refresh_red_letter_case(who, id, body.expected_version, key)


@router.post("/v1/finance/red-letter-cases/{id}/invoices", operation_id="linkRedLetterInvoice")
async def link(request: Request, id: str, body: RedLetterInvoiceLink,
               key: IdempotencyKey, who: FinanceCaller) -> dict[str, Any]:
    """真实人工关联已独立导入查验的红票，不接受模型代点。"""
    require_write(request)
    _human(who.origin)
    return await get_store(request).link_red_letter_invoice(who, id, body, key)


@router.get("/v1/finance/tax-adjustments", operation_id="listTaxAdjustments")
async def adjustments(request: Request, who: FinanceCaller, legal_entity_id: Company,
                      cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """材料收到与实际税务调整完成分别显示。"""
    return await get_store(request).list_tax_adjustments(who, legal_entity_id, cursor=cursor, limit=limit)


@router.post("/v1/finance/tax-adjustments/{id}/evidence", operation_id="recordTaxAdjustmentEvidence")
async def adjustment_evidence(request: Request, id: str, body: TaxAdjustmentEvidence,
                              key: IdempotencyKey, who: FinanceCaller) -> dict[str, Any]:
    """人工补件不产生已申报或已转出结论。"""
    require_write(request)
    _human(who.origin)
    return await get_store(request).record_tax_adjustment_evidence(who, id, body, key)


@router.get("/v1/finance/archives", operation_id="listArchives")
async def archives(request: Request, who: FinanceCaller, legal_entity_id: Company,
                   cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """仅列出当前范围内的不可变封存事实和补包缺口。"""
    return await get_store(request).list_archives(who, legal_entity_id, cursor=cursor, limit=limit)


@router.get("/v1/finance/archives/{id}", operation_id="getArchive")
async def archive(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """历史封存与当前健康状态分开返回。"""
    return await get_store(request).get_archive(who, id)


@router.get("/v1/finance/archives/{id}/manifest", operation_id="getArchiveManifest")
async def manifest(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """固定清册包含实际成员摘要与输入版本。"""
    return await get_store(request).get_archive_manifest(who, id)


@router.get("/v1/finance/archives/{id}/tests", operation_id="getArchiveTests")
async def tests(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """逐项展示真实检测结论及其明确范围。"""
    return await get_store(request).get_archive_tests(who, id)


@router.get("/v1/finance/archives/{id}/evidence", operation_id="getArchiveEvidence")
async def evidence(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """保留固定对象版本、锁与独立封存证明。"""
    return await get_store(request).get_archive_evidence(who, id)


@router.get("/v1/finance/archives/{id}/supplements", operation_id="listArchiveSupplements")
async def supplements(request: Request, id: str, who: FinanceCaller,
                       cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """补充档案追加引用父清册，原始包不会被覆盖。"""
    return await get_store(request).list_archive_supplements(who, id, cursor=cursor, limit=limit)


@router.post("/v1/finance/archives/{id}/audits", status_code=202, operation_id="auditArchive")
async def audit(request: Request, id: str, body: VersionRequest,
                key: IdempotencyKey, who: FinanceCaller) -> dict[str, Any]:
    """审计只读原档案全部对象及锁，不重新上传。"""
    return await get_store(request).audit_archive(who, id, body.expected_version, key)


@router.get("/v1/finance/archives/{id}/audits", operation_id="getArchiveAudits")
async def audits(request: Request, id: str, who: FinanceCaller,
                 cursor: Cursor = None, limit: Limit = 50) -> dict[str, Any]:
    """审计失败只追加当前健康告警，不改历史封存事实。"""
    return await get_store(request).get_archive_audits(who, id, cursor=cursor, limit=limit)


def _human(origin: str) -> None:
    """敏感人工补件和红票关联不接受agent来源。"""
    if origin != "human":
        raise FinanceError("HUMAN_REQUIRED", 403, "请本人核对后提交")
