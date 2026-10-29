"""税务、红字和人工调整的实际证据闭包，不包含归档自身未来事实。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection

TAX_ACTIONS = ["verify_invoice", "confirm_usage", "confirm_tax_entry", "confirm_red_letter", "refresh_red_letter"]


async def expand_invoice_ids(conn: Connection, tenant: str, entity: str, ids: list[str]) -> list[str]:
    """实际红票关联扩为同公司闭包，调用方随后统一排序锁票。"""
    linked = await conn.fetch("SELECT red_invoice_id FROM finance_red_letter_link WHERE tenant_id=$1 AND legal_entity_id=$2 AND blue_invoice_id=ANY($3::text[]) ORDER BY red_invoice_id", tenant, entity, ids)
    return sorted({*ids, *(r["red_invoice_id"] for r in linked)})


def serialize_record(row: Any) -> dict[str, Any]:
    """保留实际数据库事实但统一JSON与时间序列化。"""
    result = {}
    for key, value in dict(row).items():
        if key in {"data", "snapshot", "request", "request_template", "content"} and value is not None:
            value = decode(value)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[key] = value
    return result


async def compliance_history(conn: Connection, tenant: str, entity: str, ids: list[str]) -> dict[str, Any]:
    """只冻结实际税务动作已受理的原计划和人工链，排除archive/audit。"""
    operations = await conn.fetch("SELECT * FROM finance_compliance_operation o WHERE o.tenant_id=$1 AND o.legal_entity_id=$2 AND o.action=ANY($3::text[]) AND ((o.target_type='invoice' AND o.target_id=ANY($4::text[])) OR EXISTS(SELECT 1 FROM finance_red_letter_case c WHERE c.tenant_id=o.tenant_id AND c.legal_entity_id=o.legal_entity_id AND c.id=o.target_id AND c.invoice_id=ANY($4::text[]))) ORDER BY o.created_at,o.id", tenant, entity, TAX_ACTIONS, ids)
    plan_ids = [r["plan_id"] for r in operations]
    plans = await conn.fetch("SELECT * FROM finance_compliance_plan WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=ANY($3::text[]) ORDER BY created_at,id", tenant, entity, plan_ids)
    workflows = await conn.fetch("SELECT * FROM finance_compliance_workflow WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=ANY($3::text[]) ORDER BY id", tenant, entity, plan_ids)
    workflow_ids = [r["id"] for r in workflows]
    steps = await conn.fetch("SELECT * FROM finance_compliance_workflow_step WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=ANY($3::text[]) ORDER BY workflow_id,position", tenant, entity, workflow_ids)
    decisions = await conn.fetch("SELECT * FROM finance_compliance_decision WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=ANY($3::text[]) ORDER BY created_at,id", tenant, entity, workflow_ids)
    grants = await conn.fetch("SELECT * FROM finance_compliance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=ANY($3::text[]) ORDER BY plan_id", tenant, entity, plan_ids)
    operation_facts = [{k: v for k, v in serialize_record(r).items() if k not in {"lease_token", "lease_until", "query_requested", "next_query_at", "query_count", "version"}} for r in operations]
    return {"operations": operation_facts, "plans": [serialize_record(r) for r in plans], "workflows": [serialize_record(r) for r in workflows], "steps": [serialize_record(r) for r in steps], "decisions": [serialize_record(r) for r in decisions], "grants": [serialize_record(r) for r in grants]}


async def adjustment_closure(conn: Connection, tenant: str, entity: str, ids: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """调整义务、人工补件和引用原件bytes同时闭包，保持原权威状态。"""
    simulation = bool(await conn.fetchval("SELECT COALESCE((SELECT simulation FROM finance_compliance_mode WHERE singleton),(SELECT simulation FROM finance_settlement_mode WHERE singleton),false)"))
    invoices = await conn.fetch("SELECT id,source_simulation FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=ANY($3::text[])", tenant, entity, ids)
    if {r["id"] for r in invoices} != set(ids) or any(r["source_simulation"] != simulation for r in invoices):
        raise FinanceError("ARCHIVE_ADJUSTMENT_ENVIRONMENT_CONFLICT", 409, "调整义务原票来源与当前财务环境不一致")
    adjustments = await conn.fetch("SELECT * FROM finance_tax_adjustment WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=ANY($3::text[]) ORDER BY created_at,id", tenant, entity, ids)
    adjustment_ids = [r["id"] for r in adjustments]
    materials = await conn.fetch("SELECT * FROM finance_tax_adjustment_evidence WHERE tenant_id=$1 AND legal_entity_id=$2 AND adjustment_id=ANY($3::text[]) ORDER BY created_at,id", tenant, entity, adjustment_ids)
    receipt_ids = sorted({id for r in materials for id in decode(r["data"])["evidence_ids"]})
    receipts = await conn.fetch("SELECT r.id,r.object_key,r.sha256,i.content_type,i.size_bytes,o.source_simulation FROM finance_receipt r JOIN finance_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id) JOIN finance_item i ON(i.tenant_id=r.tenant_id AND i.legal_entity_id=r.legal_entity_id AND i.operation_id=r.operation_id AND i.position=r.position) WHERE r.tenant_id=$1 AND r.legal_entity_id=$2 AND r.id=ANY($3::text[]) ORDER BY r.id", tenant, entity, receipt_ids)
    if len(receipts) != len(receipt_ids):
        raise FinanceError("ARCHIVE_ADJUSTMENT_MATERIAL_REQUIRED", 409, "已记录调整补件缺少实际原件")
    if any(r["source_simulation"] != simulation for r in receipts):
        raise FinanceError("ARCHIVE_ADJUSTMENT_ENVIRONMENT_CONFLICT", 409, "调整补件原始接收环境与归档原票来源不一致")
    artifacts = []
    for receipt in receipts:
        if not receipt["object_key"] or not receipt["content_type"] or not receipt["size_bytes"]:
            raise FinanceError("ARCHIVE_ADJUSTMENT_MATERIAL_REQUIRED", 409, "调整补件缺少真实对象、大小或媒体类型")
        artifacts.append({"id": "adjustment_material_" + receipt["id"], "path": "adjustment-materials/" + receipt["id"] + ".bin", "kind": "adjustment_material", "sha256": receipt["sha256"], "media_type": receipt["content_type"], "size": receipt["size_bytes"], "source": {"object_key": receipt["object_key"]}})
    return {"adjustments": [serialize_record(r) for r in adjustments], "materials": [serialize_record(r) for r in materials]}, artifacts
