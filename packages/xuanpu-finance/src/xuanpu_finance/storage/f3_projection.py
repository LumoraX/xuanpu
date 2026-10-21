"""F1分票、F2发送和票据读取共用的有效蓝票额度投影。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f3_archive_health import archive_source_changes

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


async def invoice_projection(conn: Connection, tenant: str, entity: str, data: dict[str, Any]) -> dict[str, Any]:
    """只投影不改写票面；已开红票才减额，历史预留与消费永远保留。"""
    state = await conn.fetchrow("SELECT * FROM finance_invoice_compliance WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", tenant, entity, data["id"])
    issued = await conn.fetch("SELECT data FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND status='issued' ORDER BY id", tenant, entity, data["id"])
    red_gross = sum((Decimal(decode(r["data"])["amount"]) for r in issued), Decimal(0))
    red_tax = sum((Decimal(decode(r["data"])["tax"]) for r in issued), Decimal(0))
    gross = max(Decimal(0), Decimal(data["gross"]) - red_gross)
    tax = max(Decimal(0), Decimal(data["tax"]) - red_tax)
    used = await conn.fetchrow("SELECT COALESCE(sum(amount),0) AS amount,COALESCE(sum(tax),0) AS tax FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND state IN ('reserved','consumed')", tenant, entity, data["id"])
    assert used is not None
    shortfall = used["amount"] > gross or used["tax"] > tax
    status = state["invoice_status"] if state is not None else data["ticket_status"]
    if not data["is_red"] and gross == 0:
        status = "fully_red"
    if status == "normal" and red_gross > 0 and gross > 0:
        status = "partially_red"
    blocked = shortfall or status in {"void", "red_locked", "fully_red", "abnormal"}
    result = {**data, "effective_gross": format(gross, ".2f"), "effective_tax": format(tax, ".2f"), "issued_red_amount": format(red_gross, ".2f"), "issued_red_tax": format(red_tax, ".2f"), "available_amount": format(max(Decimal(0), gross - used["amount"]) if not blocked else Decimal(0), ".2f"), "available_tax": format(max(Decimal(0), tax - used["tax"]) if not blocked else Decimal(0), ".2f"), "allocation_shortfall": shortfall, "allocation_frozen": blocked, "compliance_status": status, "compliance_version": state["version"] if state is not None else 1, "authority_revision": state["authority_revision"] if state is not None else 0}
    result["ticket_status"] = "abnormal" if status == "void" else status
    if state is not None:
        result.update(await _tax_projection(conn, tenant, entity, state))
    result["archive_status"] = await _archive_projection(conn, tenant, entity, data["id"])
    return result


async def _tax_projection(conn: Connection, tenant: str, entity: str, state: Any) -> dict[str, Any]:
    """税务状态与查验来源有效性正交，渠道切换后旧通过须刷新。"""
    verification = state["verification_status"]
    source = await conn.fetchrow(
        "SELECT p.provider,p.configuration_version,p.generation,(r.data->>'verification_max_age_hours')::integer AS max_age FROM finance_compliance_policy c "
        "JOIN finance_compliance_policy_revision r ON(r.tenant_id=c.tenant_id AND r.legal_entity_id=c.legal_entity_id AND r.version=c.version) "
        "JOIN finance_compliance_provider p ON(p.provider=r.data->>'tax_provider' AND p.configuration_version=(r.data->>'tax_configuration_version')::integer) "
        "WHERE c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.enabled AND p.enabled AND r.data->>'tax_enabled'='true'",
        tenant, entity)
    current = source is not None and (state["provider"], state["configuration_version"],
        state["provider_generation"]) == (source["provider"], source["configuration_version"], source["generation"])
    evidence = decode(state["verification_evidence"]) if state["verification_evidence"] is not None else None
    effective_until = state["valid_until"]
    if verification == "passed" and current and evidence is not None:
        verified = evidence.get("verified_at")
        if verified is None:
            receipt = await conn.fetchval("SELECT data FROM finance_compliance_receipt WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", tenant, entity, evidence.get("raw_response_ref"))
            verified = decode(receipt).get("outcome", {}).get("observed_at") if receipt is not None else None
        if verified is not None and effective_until is not None:
            assert source is not None
            effective_until = min(effective_until, datetime.fromisoformat(verified) + timedelta(hours=source["max_age"]))
        else:
            effective_until = None
    if verification == "passed" and (not current or effective_until is None or effective_until <= datetime.now(UTC)):
        verification = "stale"
    if evidence is not None:
        evidence = {**evidence, "effective_valid_until": effective_until.isoformat() if effective_until is not None else None}
    snapshot = decode(state["snapshot"]) if state["snapshot"] is not None else {}
    usage = "confirmed" if snapshot.get("usage_status") == "confirmed" else "not_confirmed"
    entry = "confirmed" if snapshot.get("tax_entry_status") == "confirmed" else "not_confirmed"
    adjustments = snapshot.get("adjustments", [])
    if any(a["status"] in {"required", "submitted"} for a in adjustments):
        entry = "adjustment_pending"
    elif adjustments and all(a["status"] in {"completed", "cancelled"} for a in adjustments):
        entry = "adjusted"
    return {"verification_status": verification, "authority_status": "verified" if verification == "passed" else "stale" if verification == "stale" else "unverified",
        "usage_status": usage, "tax_entry_status": entry,
        "verification_evidence": evidence}


async def _archive_projection(conn: Connection, tenant: str, entity: str, id: str) -> str:
    """封存历史不变，当前补件与审计失败分别投影。"""
    archive = await conn.fetchrow(
        "SELECT a.*,h.supplement_required,h.last_audit_status FROM finance_archive a "
        "JOIN finance_archive_health h ON(h.tenant_id=a.tenant_id AND h.legal_entity_id=a.legal_entity_id AND h.archive_id=a.id) "
        "WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.target_type='invoice' AND a.target_id=$3 ORDER BY a.created_at DESC,a.id DESC LIMIT 1",
        tenant, entity, id)
    if archive is not None:
        if archive["last_audit_status"] in {"failed", "unavailable"}:
            return "exception"
        return "supplement_required" if archive["supplement_required"] or await archive_source_changes(conn, archive) else "archived"
    active = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND target_type='invoice' AND target_id=$3 AND action='archive' AND status IN ('accepted','running','dispatching','awaiting_provider','outcome_unknown'))",
        tenant, entity, id)
    return "pending" if active else "not_archived"


async def effective_line_limits(conn: Connection, tenant: str, entity: str, invoice_id: str, line_id: str, data: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """蓝票行额度扣除权威明确定位到该行的已开红冲。"""
    cases = await conn.fetch("SELECT data FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND status='issued' ORDER BY id", tenant, entity, invoice_id)
    gross = Decimal(data["amount"]) + Decimal(data["tax"])
    tax = Decimal(data["tax"])
    for row in cases:
        case = decode(row["data"])
        amounts = case["line_adjustments"]
        taxes = case.get("line_tax_adjustments", {})
        if not amounts or (Decimal(case["tax"]) > 0 and not taxes):
            raise FinanceError("RED_LINE_MAPPING_REQUIRED", 409, "已开红票缺少完整实际行映射，分行使用冻结")
        if line_id in amounts:
            gross -= Decimal(amounts[line_id])
        if line_id in taxes:
            tax -= Decimal(taxes[line_id])
        elif line_id in amounts and Decimal(case["tax"]) > 0:
            raise FinanceError("RED_LINE_MAPPING_REQUIRED", 409, "已开红票缺少明确行税额映射，分行使用冻结")
    return max(Decimal(0), gross), max(Decimal(0), tax)


def require_usable_projection(data: dict[str, Any]) -> None:
    """新分配、提交、批准和外部发送统一阻断红锁及占额缺口。"""
    if data["allocation_shortfall"]:
        raise FinanceError("INVOICE_ALLOCATION_SHORTFALL", 409, "红冲后历史预留或消费超额，保留历史并冻结新动作")
    if data["allocation_frozen"]:
        raise FinanceError("INVOICE_COMPLIANCE_BLOCKED", 409, "发票已红锁、全红、作废或异常")
