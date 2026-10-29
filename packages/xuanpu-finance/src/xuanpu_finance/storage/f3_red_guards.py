"""权威已开红冲的累计票额、税额和明确行映射守卫。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


async def validate_issued_balance(conn: Connection, invoice: Any, case: dict[str, Any]) -> None:
    """累计冲减不得超原蓝票，未知行和超行金额必须拒绝应用。"""
    if case["status"] != "issued":
        return
    invoice_data = decode(invoice["data"])
    previous = await conn.fetch("SELECT data FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND status='issued' AND external_case_id<>$4", invoice["tenant_id"], invoice["legal_entity_id"], invoice["id"], case["external_case_id"])
    cases = [decode(r["data"]) for r in previous] + [case]
    amount = sum((Decimal(c["amount"]) for c in cases), Decimal(0))
    tax = sum((Decimal(c["tax"]) for c in cases), Decimal(0))
    if amount > Decimal(invoice_data["gross"]) or tax > Decimal(invoice_data["tax"]):
        raise FinanceError("RED_ISSUED_BALANCE_EXCEEDED", 409, "权威已开红冲累计超过原蓝票毛额或税额")
    lines = {decode(r["data"])["id"]: decode(r["data"]) for r in await conn.fetch("SELECT data FROM finance_invoice_line WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", invoice["tenant_id"], invoice["legal_entity_id"], invoice["id"])}
    for entry in cases:
        validate_case_line_totals(entry, lines)
    for line_id, line in lines.items():
        gross_used = sum((Decimal(c["line_adjustments"].get(line_id, "0.00")) for c in cases), Decimal(0))
        tax_used = sum((Decimal(c.get("line_tax_adjustments", {}).get(line_id, "0.00")) for c in cases), Decimal(0))
        if gross_used > Decimal(line["amount"]) + Decimal(line["tax"]) or tax_used > Decimal(line["tax"]):
            raise FinanceError("RED_LINE_BALANCE_EXCEEDED", 409, "累计已开红冲超过明确原票行金额或税额")


def validate_case_line_totals(case: dict[str, Any], lines: dict[str, Any]) -> None:
    """给出行映射时必须完整自洽，未给映射不能伪造可用行余额。"""
    gross, taxes = case["line_adjustments"], case.get("line_tax_adjustments", {})
    if not set(gross) <= set(lines) or not set(taxes) <= set(gross):
        raise FinanceError("RED_LINE_SCOPE_MISMATCH", 409, "红冲映射指向不存在或不属于原蓝票的行")
    if gross and sum((Decimal(v) for v in gross.values()), Decimal(0)) != Decimal(case["amount"]):
        raise FinanceError("RED_LINE_TOTAL_MISMATCH", 409, "红冲行含税金额合计与确认单不符")
    if taxes and sum((Decimal(v) for v in taxes.values()), Decimal(0)) != Decimal(case["tax"]):
        raise FinanceError("RED_LINE_TOTAL_MISMATCH", 409, "红冲行税额合计与确认单不符")
    if any(Decimal(v) > Decimal(gross[id]) for id, v in taxes.items()):
        raise FinanceError("RED_LINE_TOTAL_MISMATCH", 409, "红冲行税额不能超过含税金额")
