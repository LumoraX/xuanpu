"""归档所需解析明细、字段来源与真实付款凭证材料的固定清册。"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


async def structured_invoice_sources(conn: Connection, tenant: str, entity: str, invoice_ids: list[str]) -> dict[str, Any]:
    """每张原件必须同时包含实际解析明细和逐字段来源，不能空造结构化材料。"""
    result: dict[str, Any] = {}
    for invoice_id in invoice_ids:
        lines = await conn.fetch("SELECT data FROM finance_invoice_line WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 ORDER BY position", tenant, entity, invoice_id)
        fields = await conn.fetch("SELECT data FROM finance_invoice_field WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 ORDER BY position", tenant, entity, invoice_id)
        if not lines or not fields:
            raise FinanceError("ARCHIVE_STRUCTURED_REQUIRED", 409, "归档缺少原票实际解析明细或字段来源")
        result[invoice_id] = {"lines": [decode(r["data"]) for r in lines], "fields": [decode(r["data"]) for r in fields]}
    return result


async def settlement_archive_sources(conn: Connection, tenant: str, entity: str, document_id: str) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """成功付款和记账必须包含真实回单/凭证，缺少大小或文件不能降级为原回执。"""
    operations = await conn.fetch("SELECT id,action,status,request FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 ORDER BY created_at,id", tenant, entity, document_id)
    artifacts: list[dict[str, Any]] = []
    required: set[str] = set()
    snapshots = []
    for operation in operations:
        snapshots.append({"id": operation["id"], "action": operation["action"], "status": operation["status"], "request": decode(operation["request"]) if operation["request"] is not None else None})
        role = "payment_receipt" if operation["action"] == "execute_payment" else "voucher_evidence"
        receipts = await conn.fetch("SELECT * FROM finance_settlement_receipt WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 ORDER BY created_at,id", tenant, entity, operation["id"])
        documents = []
        for receipt in receipts:
            raw = bytes(receipt["raw"])
            artifacts.append({"id": "settlement_receipt_" + receipt["id"], "path": "settlement/" + receipt["id"] + ".bin", "kind": "settlement_raw_receipt", "sha256": receipt["sha256"], "size": len(raw), "media_type": "application/octet-stream", "source": {"content_base64": base64.b64encode(raw).decode()}})
            data = decode(receipt["data"])
            if operation["request"] is None or data.get("status") != "succeeded":
                continue
            for position, evidence in enumerate(data.get("evidence", [])):
                if evidence.get("size") is None:
                    raise FinanceError("ARCHIVE_PROVIDER_DOCUMENT_REQUIRED", 409, "实际回单或凭证缺少真实大小，不能用回执JSON替代")
                documents.append({"id": "settlement_evidence_" + receipt["id"] + "_" + str(position), "path": "settlement-evidence/" + receipt["id"] + "_" + str(position) + ".bin", "kind": role, "sha256": evidence["sha256"], "size": evidence["size"], "media_type": evidence["media_type"], "source": {"provider_request": decode(operation["request"]), "evidence": evidence, "provider_kind": "settlement"}})
        if operation["status"] == "succeeded":
            if not documents:
                raise FinanceError("ARCHIVE_PROVIDER_DOCUMENT_REQUIRED", 409, "成功付款或记账缺少真实回单/凭证文件")
            required.add(role)
        artifacts.extend(documents)
    return artifacts, sorted(required), snapshots
