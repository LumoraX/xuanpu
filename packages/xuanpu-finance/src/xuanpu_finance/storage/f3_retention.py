"""归档保留起点必须来自明确策略和实际日期事实。"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


async def retention_anchor(conn: Connection, target: dict[str, Any], policy: dict[str, Any], frozen: str | None) -> str:
    """缺少发票日期或实际会计期间时阻断，不能静默改用当前时间。"""
    if frozen is not None:
        return frozen
    kind = policy["retention_anchor"]
    if kind == "archived_at":
        return datetime.now(UTC).isoformat()
    if kind == "invoice_issued_at":
        if target["type"] != "invoice" or not target["data"].get("issue_date"):
            raise FinanceError("ARCHIVE_RETENTION_ANCHOR_UNAVAILABLE", 409, "该目标缺少唯一实际开票日期保留起点")
        return datetime.fromisoformat(target["data"]["issue_date"]).replace(tzinfo=UTC).isoformat()
    period = None
    if target["type"] == "invoice":
        snapshot = target["compliance"]["snapshot"]
        period = snapshot.get("accounting_period") if snapshot is not None else None
    else:
        data = await conn.fetchval("SELECT data FROM finance_posting WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND status='succeeded' AND original_posting_id IS NULL ORDER BY created_at DESC,id DESC LIMIT 1", target["tenant_id"], target["legal_entity_id"], target["id"])
        period = decode(data).get("accounting_period") if data is not None else None
    if period is None:
        raise FinanceError("ARCHIVE_RETENTION_ANCHOR_UNAVAILABLE", 409, "期间末保留起点缺少实际税务或记账期间")
    year, month = (int(value) for value in period.split("-"))
    return datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=UTC).isoformat()
