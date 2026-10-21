"""由实际持久原件、回执和业务事实构造不可变归档输入。"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f3_archive_health import archive_aggregate_ids, snapshot_cutoff_sequence
from xuanpu_finance.storage.f3_archive_sources import (
    settlement_archive_sources,
    structured_invoice_sources,
)
from xuanpu_finance.storage.f3_evidence_closure import (
    adjustment_closure,
    compliance_history,
    expand_invoice_ids,
)
from xuanpu_finance.storage.f3_retention import retention_anchor
from xuanpu_finance.storage.f3_targets import ComplianceTargetStore

if TYPE_CHECKING:
    from xuanpu_finance.compliance.models import ComplianceActionPreview
    from xuanpu_finance.storage.business_base import Connection


def derived_artifact(id: str, kind: str, value: object) -> dict[str, Any]:
    """数据库派生JSON明确标记derived，不冒充官方原始格式。"""
    raw = json.dumps({"source": "derived", "data": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"id": id, "path": "derived/" + id + ".json", "kind": kind, "sha256": hashlib.sha256(raw).hexdigest(), "media_type": "application/json", "size": len(raw), "source": {"content_base64": base64.b64encode(raw).decode()}}


class ComplianceArchiveInputStore(ComplianceTargetStore):
    """归档只使用数据库已授权的固定来源，不接受任意URL。"""

    async def _f3_archive_snapshot(self, conn: Connection, target: dict[str, Any], body: ComplianceActionPreview, policy: dict[str, Any], *, anchor: str | None = None) -> dict[str, Any]:
        """冻结全部目标、合规版本、实际清册与保留预期。"""
        tenant, entity = target["tenant_id"], target["legal_entity_id"]
        invoice_ids = [target["id"]] if target["type"] == "invoice" else [i["id"] for i in target.get("invoices", [])]
        invoice_ids = await expand_invoice_ids(conn, tenant, entity, invoice_ids)
        aggregate_ids = await archive_aggregate_ids(conn, tenant, entity, target["type"], target["id"])
        latest_event = await conn.fetchrow("SELECT sequence,created_at FROM finance_event WHERE tenant_id=$1 AND legal_entity_id=$2 AND aggregate_id=ANY($3::text[]) AND kind NOT LIKE 'compliance.archive%' ORDER BY sequence DESC LIMIT 1", tenant, entity, aggregate_ids)
        cutoff_sequence = latest_event["sequence"] if latest_event is not None else 0
        cutoff = latest_event["created_at"] if latest_event is not None else await conn.fetchval("SELECT min(received_at) FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=ANY($3::text[])", tenant, entity, invoice_ids)
        if cutoff is None:
            raise FinanceError("ARCHIVE_SOURCE_REQUIRED", 409, "归档目标缺少实际来源事件或原件")
        structured = await structured_invoice_sources(conn, tenant, entity, invoice_ids)
        artifacts = [derived_artifact("target", "structured_record", target), derived_artifact("invoice-lines-fields", "invoice_structure", structured)]
        adjustments, adjustment_artifacts = await adjustment_closure(conn, tenant, entity, invoice_ids)
        history = await compliance_history(conn, tenant, entity, invoice_ids)
        artifacts.extend([derived_artifact("compliance-history", "compliance_history", history), derived_artifact("tax-adjustments", "tax_adjustments", adjustments), *adjustment_artifacts])
        originals = await self._f3_archive_originals(conn, tenant, entity, invoice_ids)
        if not originals:
            raise FinanceError("ARCHIVE_ORIGINAL_REQUIRED", 409, "归档必须含真实原件")
        artifacts.extend(originals)
        receipts = await self._f3_archive_receipts(conn, tenant, entity, invoice_ids)
        artifacts.extend(receipts)
        histories = await conn.fetch("SELECT kind,data,sequence,created_at FROM finance_event WHERE tenant_id=$1 AND legal_entity_id=$2 AND aggregate_id=ANY($3::text[]) AND sequence<=$4 AND kind NOT LIKE 'compliance.archive%' ORDER BY sequence", tenant, entity, aggregate_ids, cutoff_sequence)
        events = [{"id": str(r["sequence"]), "kind": r["kind"], "created_at": r["created_at"].isoformat(), "data": decode(r["data"])} for r in histories]
        artifacts.append(derived_artifact("events", "audit_events", events))
        required = ["structured_record", "invoice_original", "audit_events", "invoice_structure", "compliance_history", "tax_adjustments"]
        settlement_snapshots: list[dict[str, Any]] = []
        documents = await conn.fetch("SELECT * FROM finance_expense_claim WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=ANY($3::text[]) ORDER BY id", tenant, entity, aggregate_ids)
        document_snapshots = [{"id": d["id"], "version": d["version"], "status": d["status"], "amount": format(d["total"], ".2f"), "content": decode(d["content"])} for d in documents]
        for document in documents:
            document_id = document["id"]
            settlement_artifacts, settlement_roles, settlement_data = await settlement_archive_sources(conn, tenant, entity, document_id)
            artifacts.extend(settlement_artifacts)
            required.extend(settlement_roles)
            settlement_snapshots.extend(settlement_data)
            decisions = await conn.fetch("SELECT d.* FROM finance_decision d JOIN finance_workflow w ON(w.tenant_id=d.tenant_id AND w.legal_entity_id=d.legal_entity_id AND w.id=d.workflow_id) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.document_id=$3 ORDER BY d.created_at,d.id", tenant, entity, document_id)
            decision_data = [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()} for r in decisions]
            artifacts.append(derived_artifact("decisions-" + document_id, "approval_decisions", decision_data))
            required.append("approval_decisions")
        if documents:
            artifacts.append(derived_artifact("related-documents", "related_documents", document_snapshots))
            required.append("related_documents")
        parent = await self._f3_archive_parent(conn, target, body, events)
        snapshot: dict[str, Any] = {"target": {"id": target["id"], "type": target["type"], "version": target["version"]}, "content_version": target.get("content_version", target["version"]), "compliance_version": target["compliance_version"], "event_cutoff": cutoff.isoformat(), "simulation": target["simulation"], "profile": {"id": policy["archive_profile_id"], "version": policy["archive_profile_version"]}, "retention": {"retention_class": policy["retention_class"], "days": policy["retention_days"], "anchor_rule": policy["retention_anchor"], "anchor_at": await retention_anchor(conn, target, policy, anchor), "required_lock_mode": policy["required_lock_mode"], "required_legal_hold": policy["required_legal_hold"]}, "parent_manifest_hash": parent, "snapshots": {"target": target, "events": events, "invoice_structure": structured, "compliance_history": history, "tax_adjustments": adjustments, "settlements": settlement_snapshots, "related_documents": document_snapshots, "aggregate_ids": aggregate_ids, "event_cutoff_sequence": cutoff_sequence}, "required_roles": sorted(set(required)), "artifacts": artifacts}
        snapshot["source_snapshot_hash"] = request_digest(snapshot)
        return snapshot

    async def _f3_archive_originals(self, conn: Connection, tenant: str, entity: str, invoice_ids: list[str]) -> list[dict[str, Any]]:
        """原件来自加密原件库实际对象和已接收大小，不推导官方格式。"""
        rows = await conn.fetch("SELECT DISTINCT r.id,r.object_key,r.sha256,i.content_type,i.size_bytes,o.source_simulation,f.source_simulation AS invoice_simulation FROM finance_invoice_receipt l JOIN finance_invoice f ON(f.tenant_id=l.tenant_id AND f.legal_entity_id=l.legal_entity_id AND f.id=l.invoice_id) JOIN finance_receipt r ON(r.tenant_id=l.tenant_id AND r.legal_entity_id=l.legal_entity_id AND r.id=l.receipt_id) JOIN finance_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id) JOIN finance_item i ON(i.tenant_id=r.tenant_id AND i.legal_entity_id=r.legal_entity_id AND i.operation_id=r.operation_id AND i.position=r.position) WHERE l.tenant_id=$1 AND l.legal_entity_id=$2 AND l.invoice_id=ANY($3::text[]) ORDER BY r.id", tenant, entity, invoice_ids)
        simulation = await self._simulation(conn)
        if any(r["source_simulation"] != simulation or r["invoice_simulation"] != simulation for r in rows):
            raise FinanceError("ARCHIVE_ORIGINAL_ENVIRONMENT_CONFLICT", 409, "原票及实际原件操作来源与当前归档环境不一致")
        artifacts = []
        for row in rows:
            if not row["object_key"] or row["size_bytes"] is None or row["size_bytes"] <= 0 or not row["content_type"]:
                raise FinanceError("ARCHIVE_ORIGINAL_INVALID", 409, "原件缺少实际大小、类型或对象引用")
            artifacts.append({"id": "original_" + row["id"], "path": "originals/" + row["id"] + ".bin", "kind": "invoice_original", "sha256": row["sha256"], "media_type": row["content_type"], "size": row["size_bytes"], "source": {"object_key": row["object_key"]}})
        return artifacts

    async def _f3_archive_receipts(self, conn: Connection, tenant: str, entity: str, invoice_ids: list[str]) -> list[dict[str, Any]]:
        """原始回执与供应商证据均绑定实际持久请求，禁止任意URL。"""
        rows = await conn.fetch("SELECT r.*,o.request FROM finance_compliance_receipt r JOIN finance_compliance_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id) WHERE o.tenant_id=$1 AND o.legal_entity_id=$2 AND o.action IN ('verify_invoice','confirm_usage','confirm_tax_entry','confirm_red_letter','refresh_red_letter') AND ((o.target_type='invoice' AND o.target_id=ANY($3::text[])) OR EXISTS(SELECT 1 FROM finance_red_letter_case c WHERE c.tenant_id=o.tenant_id AND c.legal_entity_id=o.legal_entity_id AND c.id=o.target_id AND c.invoice_id=ANY($3::text[]))) ORDER BY r.created_at,r.id", tenant, entity, invoice_ids)
        artifacts = []
        for row in rows:
            raw = bytes(row["raw"])
            artifacts.append({"id": "receipt_" + row["id"], "path": "receipts/" + row["id"] + ".bin", "kind": "tax_receipt", "sha256": row["sha256"], "media_type": "application/octet-stream", "size": len(raw), "source": {"content_base64": base64.b64encode(raw).decode()}})
            if row["stale"] or row["request"] is None:
                continue
            result = decode(row["data"])
            for index, evidence in enumerate(result.get("evidence", [])):
                if evidence.get("size") is None:
                    if result.get("status") == "succeeded":
                        raise FinanceError("ARCHIVE_PROVIDER_DOCUMENT_REQUIRED", 409, "税务成功证据缺少实际大小，不能只归档回执JSON")
                    continue
                artifacts.append({"id": "evidence_" + row["id"] + "_" + str(index), "path": "evidence/" + row["id"] + "_" + str(index) + ".bin", "kind": "tax_evidence", "sha256": evidence["sha256"], "media_type": evidence["media_type"], "size": evidence["size"], "source": {"provider_request": decode(row["request"]), "evidence": evidence, "provider_kind": "compliance"}})
        return artifacts

    async def _f3_archive_parent(self, conn: Connection, target: dict[str, Any], body: ComplianceActionPreview, events: list[dict[str, Any]]) -> str | None:
        """补充封存绑定同目标父清册和未消费的新来源事件。"""
        parent = body.parameters.parent_manifest_hash
        if parent is None:
            return None
        row = await conn.fetchrow("SELECT * FROM finance_archive WHERE tenant_id=$1 AND legal_entity_id=$2 AND target_type=$3 AND target_id=$4 AND manifest_hash=$5", target["tenant_id"], target["legal_entity_id"], target["type"], target["id"], parent)
        source_ids = body.parameters.source_event_ids or []
        if row is None or not source_ids or not set(source_ids) <= {e["id"] for e in events if int(e["id"]) > snapshot_cutoff_sequence(decode(row["snapshot"]))}:
            raise FinanceError("ARCHIVE_SUPPLEMENT_SOURCE_INVALID", 409, "补包须绑定同目标父清册和其后实际新事件")
        used = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_archive_supplement WHERE tenant_id=$1 AND legal_entity_id=$2 AND archive_id=$3 AND source_event_id=ANY($4::text[]))", target["tenant_id"], target["legal_entity_id"], row["id"], source_ids)
        if used:
            raise FinanceError("ARCHIVE_SUPPLEMENT_DUPLICATE", 409, "父清册的来源事件已被补包消费")
        return parent
