"""验签后F3回调去重、异文原件保全与只查询调度。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.settlement.provider import _unique_object
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f3_workflow_queries import ComplianceWorkflowQueryStore


class ComplianceStore(ComplianceWorkflowQueryStore):
    """回调永远只触发原请求GET，不接受外部成功字符串直接推进业务。"""

    async def accept_compliance_event(self, provider: str, configuration_version: int, event_id: str, raw: bytes, body: dict[str, Any]) -> dict[str, Any]:
        """固定配置范围内保全原文，重复异文单独保存并进入人工核对。"""
        if len(raw) > 1048576:
            raise FinanceError("CALLBACK_RAW_INVALID", 422, "回调原文超出保全上限")
        raw_hash = hashlib.sha256(raw).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", request_digest([provider, configuration_version, event_id]))
            config = await conn.fetchrow("SELECT * FROM finance_compliance_provider WHERE provider=$1 AND configuration_version=$2", provider, configuration_version)
            if config is None:
                raise FinanceError("PROVIDER_NOT_FOUND", 404, "固定合规渠道不存在")
            old = await conn.fetchrow("SELECT * FROM finance_compliance_inbox WHERE provider=$1 AND configuration_version=$2 AND event_id=$3", provider, configuration_version, event_id)
            if old is not None:
                if old["raw_hash"] == raw_hash:
                    return {"status": "duplicate", "event_id": event_id}
                await conn.execute("INSERT INTO finance_compliance_inbox_conflict(id,provider,configuration_version,event_id,raw_hash,raw) VALUES($1,$2,$3,$4,$5,$6)", identifier(), provider, configuration_version, event_id, raw_hash, raw)
                return {"status": "conflict", "event_id": event_id}
            row = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND provider=$3 AND configuration_version=$4 AND external_request_id=$5", config["tenant_id"], config["legal_entity_id"], provider, configuration_version, body.get("external_request_id"))
            valid = row is not None and row["request"] is not None
            if valid:
                assert row is not None
                request = decode(row["request"])
                mandatory = ("tenant_id", "legal_entity_id", "provider", "configuration_version", "simulation", "external_request_id")
                optional = ("operation_id", "action", "amount", "currency", "payload_hash", "target", "plan_digest")
                valid = all(body.get(k) == request[k] for k in mandatory) and all(k not in body or body[k] == request[k] for k in optional)
                try:
                    valid = valid and json.loads(raw, object_pairs_hook=_unique_object) == body
                except (ValueError, UnicodeDecodeError):
                    valid = False
            status = "accepted" if valid else "rejected"
            await conn.execute("INSERT INTO finance_compliance_inbox(provider,configuration_version,event_id,raw_hash,raw,status,operation_id) VALUES($1,$2,$3,$4,$5,$6,$7)", provider, configuration_version, event_id, raw_hash, raw, status, row["id"] if row is not None else None)
            if valid:
                assert row is not None
                await conn.execute("UPDATE finance_compliance_operation SET query_requested=true,next_query_at=now() WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND request IS NOT NULL AND status IN ('dispatching','awaiting_provider','outcome_unknown')", row["tenant_id"], row["legal_entity_id"], row["id"])
            return {"status": status, "event_id": event_id, "operation_id": row["id"] if row is not None else None}
