"""按实际持久事件序号比较封存后变化，不反向取得单据锁。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f3_evidence_closure import expand_invoice_ids
from xuanpu_finance.storage.f5_archive import funding_family

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


def snapshot_cutoff_sequence(snapshot: dict[str, Any]) -> int:
    """旧包可由已冻结事件ID恢复序号，新包显式冻结同一序号。"""
    body = snapshot["snapshots"]
    if "event_cutoff_sequence" in body:
        return int(body["event_cutoff_sequence"])
    return max((int(e["id"]) for e in body["events"]), default=0)


async def archive_aggregate_ids(conn: Connection, tenant: str, entity: str, target_type: str, target_id: str, snapshot: dict[str, Any] | None = None) -> list[str]:
    """关联单据、票据和历史已冻结目标只读汇总，不取得反向单据行锁。"""
    if target_type == "invoice":
        invoices = [target_id]
        documents = [r["document_id"] for r in await conn.fetch("SELECT DISTINCT document_id FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", tenant, entity, target_id)]
    else:
        documents = [target_id]
        invoices = [r["invoice_id"] for r in await conn.fetch("SELECT DISTINCT invoice_id FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3", tenant, entity, target_id)]
    documents = await funding_family(conn, tenant, entity, documents)
    linked = await conn.fetch("SELECT DISTINCT invoice_id FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=ANY($3::text[])", tenant, entity, documents)
    invoices = await expand_invoice_ids(conn, tenant, entity, sorted({*invoices, *(r["invoice_id"] for r in linked)}))
    frozen = snapshot["snapshots"].get("aggregate_ids", []) if snapshot is not None else []
    return sorted({target_id, *invoices, *documents, *frozen})


async def archive_source_changes(conn: Connection, archive: Any) -> list[dict[str, Any]]:
    """统一供档案读取、票据投影和封存结果提交比较实际新事实。"""
    snapshot = decode(archive["snapshot"])
    tenant, entity = archive["tenant_id"], archive["legal_entity_id"]
    ids = await archive_aggregate_ids(conn, tenant, entity, archive["target_type"], archive["target_id"], snapshot)
    rows = await conn.fetch("SELECT e.sequence,e.kind,e.created_at FROM finance_event e WHERE e.tenant_id=$1 AND e.legal_entity_id=$2 AND e.aggregate_id=ANY($3::text[]) AND e.sequence>$4 AND e.kind NOT LIKE 'compliance.archive%' AND NOT EXISTS(SELECT 1 FROM finance_archive_supplement s WHERE s.tenant_id=e.tenant_id AND s.legal_entity_id=e.legal_entity_id AND s.archive_id=$5 AND s.source_event_id=e.sequence::text) ORDER BY e.sequence", tenant, entity, ids, snapshot_cutoff_sequence(snapshot), archive["id"])
    return [{"id": str(r["sequence"]), "summary": r["kind"], "created_at": r["created_at"].isoformat()} for r in rows]
