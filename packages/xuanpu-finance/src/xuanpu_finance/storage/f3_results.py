"""F3原始回执保全、单调税务事实与封存结果的原子提交。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_finance.compliance.archive_models import ArchiveOutcome
from xuanpu_finance.compliance.models import TaxSnapshot, validate_json_text
from xuanpu_finance.compliance.protocol import ComplianceResult
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f2_results import RESULT_SCOPE
from xuanpu_finance.storage.f3_archive_guards import validate_archive_outcome
from xuanpu_finance.storage.f3_archive_health import archive_source_changes
from xuanpu_finance.storage.f3_authority import authority_position, same_authority
from xuanpu_finance.storage.f3_evidence_closure import expand_invoice_ids
from xuanpu_finance.storage.f3_execution import ComplianceExecutionStore
from xuanpu_finance.storage.f3_red_guards import validate_issued_balance

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


class ComplianceResultStore(ComplianceExecutionStore):
    """撤销授权不抹去已发送动作的外部真实结果，旧租约仅保留原件。"""

    async def _f3_lock_subject(self, conn: Connection, tenant: str, entity: str, kind: str, id: str) -> None:
        """只按固定目标锁数据，不以当前员工授权拒绝保存既有外部事实。"""
        if kind in {"expense_claim", "payable"}:
            await conn.fetchrow("SELECT id FROM finance_expense_claim WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE", tenant, entity, id)
            rows = await conn.fetch("SELECT DISTINCT invoice_id FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 ORDER BY invoice_id", tenant, entity, id)
            ids = [r["invoice_id"] for r in rows]
        elif kind == "red_letter_case":
            invoice_id = await conn.fetchval("SELECT invoice_id FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", tenant, entity, id)
            ids = [invoice_id] if invoice_id is not None else []
        elif kind == "archive":
            archive = await conn.fetchrow("SELECT target_type,target_id FROM finance_archive WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", tenant, entity, id)
            if archive is not None:
                await self._f3_lock_subject(conn, tenant, entity, archive["target_type"], archive["target_id"])
            return
        else:
            ids = [id]
        ids = await expand_invoice_ids(conn, tenant, entity, ids)
        for invoice_id in sorted(ids):
            await conn.fetchrow("SELECT id FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE", tenant, entity, invoice_id)
        for invoice_id in sorted(ids):
            await conn.execute("INSERT INTO finance_invoice_compliance(tenant_id,legal_entity_id,invoice_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", tenant, entity, invoice_id)
            await conn.fetchrow("SELECT invoice_id FROM finance_invoice_compliance WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 FOR UPDATE", tenant, entity, invoice_id)

    async def _f3_worker_lock(self, conn: Connection, job: dict[str, Any]) -> Any:
        """目标先于合规操作行，全部人工与worker保持相同锁序。"""
        current = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", job["tenant_id"], job["legal_entity_id"], job["id"])
        if current is None:
            raise FinanceError("LEASE_LOST", 409, "合规操作不存在")
        await self._f3_lock_subject(conn, current["tenant_id"], current["legal_entity_id"], current["target_type"], current["target_id"])
        row = await conn.fetchrow("SELECT *,lease_until>now() AS lease_active FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE", job["tenant_id"], job["legal_entity_id"], job["id"])
        if row is None or row["lease_token"] != job["lease_token"] or not row["lease_active"] or row["status"] in {"succeeded", "failed", "cancelled"}:
            raise FinanceError("LEASE_LOST", 409, "合规租约fencing令牌已经失效")
        return row

    async def record_compliance_result(self, job: dict[str, Any], result: dict[str, Any], raw: bytes) -> None:
        """原字节先保全；范围、协议或业务事实不符只标未知且保留证据。"""
        if len(raw) > 1048576:
            raise FinanceError("PROVIDER_RESPONSE_TOO_LARGE", 422, "合规回执超过保全上限")
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                row = await self._f3_worker_lock(conn, job)
                receipt_id = await self._f3_save_receipt(conn, row, result, raw)
                await self._f3_apply_result(conn, row, result, receipt_id, queried=job["mode"] == "query")
        except FinanceError as exc:
            if exc.code != "LEASE_LOST":
                raise
            await self._f3_stale_receipt(job, result, raw)

    async def _f3_save_receipt(self, conn: Connection, row: Any, result: dict[str, Any], raw: bytes, *, stale: bool = False) -> str:
        """不可变原始回执保留当前结果及独立读回错误证据。"""
        receipt_id = "f3receipt_" + identifier()
        try:
            validate_json_text(result)
            metadata = json.dumps(result, allow_nan=False)
        except (ValueError, TypeError) as exc:
            metadata = json.dumps({"invalid_result_metadata": True, "code": "PROVIDER_JSON_INVALID", "error_type": type(exc).__name__})
        await conn.execute("INSERT INTO finance_compliance_receipt(tenant_id,legal_entity_id,id,operation_id,raw,sha256,data,stale) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8)", row["tenant_id"], row["legal_entity_id"], receipt_id, row["id"], raw, hashlib.sha256(raw).hexdigest(), metadata, stale)
        if row["action"] not in {"archive", "audit_archive", "approve", "reject"}:
            target_id = row["target_id"]
            if row["target_type"] == "red_letter_case":
                target_id = await conn.fetchval("SELECT invoice_id FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["target_id"])
            await self._event(conn, {"tenant_id": row["tenant_id"], "legal_entity_id": row["legal_entity_id"], "id": target_id}, "compliance.receipt_recorded", {"receipt_id": receipt_id, "operation_id": row["id"], "stale": stale})
        return receipt_id

    async def _f3_stale_receipt(self, job: dict[str, Any], result: dict[str, Any], raw: bytes) -> None:
        """过期worker只能向自己曾持有的原任务追加stale原件。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", job["tenant_id"], job["legal_entity_id"], job["id"])
            valid = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_compliance_lease WHERE lease_token=$1 AND tenant_id=$2 AND legal_entity_id=$3 AND operation_id=$4)", job["lease_token"], job["tenant_id"], job["legal_entity_id"], job["id"])
            if row is None or row["request"] is None or not valid or any(job.get(k) != row[k] for k in ("provider", "configuration_version", "action")):
                raise FinanceError("STALE_RECEIPT_SCOPE_INVALID", 409, "过期回执不属于原请求及租约")
            await self._f3_save_receipt(conn, row, result, raw, stale=True)

    async def _f3_apply_result(self, conn: Connection, row: Any, result: dict[str, Any], receipt_id: str, *, queried: bool) -> None:
        """二次校验固定信封和业务状态，疑义事实保全后进入只查不重发。"""
        request = decode(row["request"]) if row["request"] is not None else None
        status, error = "outcome_unknown", None
        try:
            validate_json_text(result)
            json.dumps(result, allow_nan=False)
            if request is None or any(result.get(k) != request[k] for k in RESULT_SCOPE):
                raise FinanceError("PROVIDER_SCOPE_MISMATCH", 409, "合规回执与固定请求范围不符")
            validated = ComplianceResult.model_validate({k: v for k, v in result.items() if k != "readback_evidence"})
            if validated.status == "succeeded":
                if result.get("readback_evidence"):
                    raise FinanceError("ARCHIVE_READBACK_UNVERIFIED", 409, "实际读回失败不能接受成功")
                async with conn.transaction():
                    await self._f3_commit_outcome(conn, row, result, receipt_id, request)
                status = "succeeded"
            elif validated.status == "failed":
                status = "failed"
            elif validated.status in {"accepted", "pending"}:
                status = "awaiting_provider"
        except (FinanceError, ValidationError, ValueError, TypeError, KeyError) as exc:
            error = exc.code if isinstance(exc, FinanceError) else "PROVIDER_OUTCOME_INVALID"
        await self._f3_schedule_result(conn, row, status, error, queried=queried)
        if error is not None:
            await self._f3_exception(conn, row, error, {"receipt_id": receipt_id})
        if row["action"] == "audit_archive" and status != "succeeded":
            health = "failed" if status == "failed" else "unavailable"
            data = {"status": health, "receipt_id": receipt_id, "failure_code": error or result.get("failure_code"), "readback_evidence": result.get("readback_evidence", [])}
            await conn.execute("INSERT INTO finance_archive_audit(tenant_id,legal_entity_id,id,owner_id,archive_id,operation_id,data) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)", row["tenant_id"], row["legal_entity_id"], identifier(), row["owner_id"], row["target_id"], row["id"], json.dumps(data))
            await conn.execute("UPDATE finance_archive_health SET last_audit_status=$4 WHERE tenant_id=$1 AND legal_entity_id=$2 AND archive_id=$3", row["tenant_id"], row["legal_entity_id"], row["target_id"], health)

    async def _f3_schedule_result(self, conn: Connection, row: Any, status: str, error: str | None, *, queried: bool) -> None:
        """所有非终态回执也累计查询次数，四次后等人工核对。"""
        count = row["query_count"] + int(queried)
        delay = (60, 300, 900, 3600)[min(count, 3)]
        await conn.execute("UPDATE finance_compliance_operation SET status=$4,version=version+1,business_result=$5,last_error=$6,query_count=$7,next_query_at=CASE WHEN $4 IN ('outcome_unknown','awaiting_provider') AND $7<4 THEN now()+($8::integer*interval '1 second') ELSE NULL END WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["id"], status, "completed" if status == "succeeded" else "failed" if status == "failed" else "pending", error, count, delay)
        if count >= 4 and status in {"awaiting_provider", "outcome_unknown"}:
            await self._f3_exception(conn, row, "MANUAL_RECONCILIATION_REQUIRED", {"code": error})

    async def _f3_commit_outcome(self, conn: Connection, row: Any, result: dict[str, Any], receipt_id: str, request: dict[str, Any]) -> None:
        """税务与归档成功分别接受严格DTO和原冻结目标清册。"""
        if row["action"] in {"archive", "audit_archive"}:
            outcome = ArchiveOutcome.model_validate(result["outcome"])
            await self._f3_commit_archive(conn, row, outcome, request)
            return
        snapshot = TaxSnapshot.model_validate(result["outcome"])
        await self._f3_tax_action_semantics(row, request, snapshot)
        invoice_id = row["target_id"]
        if row["target_type"] == "red_letter_case":
            invoice_id = await conn.fetchval("SELECT invoice_id FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["target_id"])
        invoice = await conn.fetchrow("SELECT * FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], invoice_id)
        if invoice is None:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "税务回执目标票据不存在")
        state = await self._f3_state(conn, invoice, lock=True)
        applied, generation = await authority_position(conn, row, invoice_id, snapshot.authority_revision)
        await conn.execute("INSERT INTO finance_invoice_verification(tenant_id,legal_entity_id,id,owner_id,invoice_id,receipt_id,authority_revision,data,applied,provider,configuration_version,provider_generation) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12)", row["tenant_id"], row["legal_entity_id"], identifier(), invoice["owner_id"], invoice_id, receipt_id, snapshot.authority_revision, snapshot.model_dump_json(), applied, row["provider"], row["configuration_version"], generation)
        if applied:
            await self._f3_apply_tax_snapshot(conn, row, invoice, state, snapshot, receipt_id, generation)
        elif same_authority(state, row, generation) and snapshot.authority_revision == state["authority_revision"] and state["snapshot"] is not None and request_digest(snapshot.model_dump(mode="json")) != request_digest(state["snapshot"]):
            await self._f3_exception(conn, row, "AUTHORITY_REVISION_CONFLICT", {"receipt_id": receipt_id})
        if row["action"] in {"confirm_usage", "confirm_tax_entry", "confirm_red_letter"}:
            await conn.execute("INSERT INTO finance_tax_confirmation(tenant_id,legal_entity_id,id,owner_id,invoice_id,operation_id,data) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)", row["tenant_id"], row["legal_entity_id"], identifier(), invoice["owner_id"], invoice_id, row["id"], json.dumps({"action": row["action"], "applied": applied, "snapshot": snapshot.model_dump(mode="json")}))

    async def _f3_tax_action_semantics(self, row: Any, request: dict[str, Any], snapshot: TaxSnapshot) -> None:
        """权威成功必须匹配具体用途、期间或原确认单，不把确认视为开票。"""
        if datetime.fromisoformat(snapshot.observed_at) > datetime.now(UTC) + timedelta(minutes=5):
            raise FinanceError("AUTHORITY_TIME_INVALID", 409, "权威快照观察时间不能来自未来")
        action = row["action"]
        params = request["payload"]["parameters"]
        if action in {"confirm_usage", "confirm_tax_entry"}:
            status = snapshot.usage_status if action == "confirm_usage" else snapshot.tax_entry_status
            if status != "confirmed" or snapshot.accounting_period != params["accounting_period"]:
                raise FinanceError("TAX_CONFIRMATION_SCOPE_MISMATCH", 409, "税务确认结果与固定期间或动作不符")
        if action == "confirm_red_letter":
            expected = request["payload"]["target_snapshot"]["data"]
            matching = [case for case in snapshot.red_letter_cases if case.external_case_id == expected["external_case_id"] and case.confirmation_id == params["confirmation_id"]]
            if len(matching) != 1 or matching[0].status not in {"confirmed", "issued"}:
                raise FinanceError("RED_CONFIRMATION_MISMATCH", 409, "权威回执未证实原确认单已经确认")

    async def _f3_apply_tax_snapshot(self, conn: Connection, row: Any, invoice: Any, state: dict[str, Any], snapshot: TaxSnapshot, receipt_id: str, generation: int) -> None:
        """只推进权威新版本，pending不得覆盖仍有效的passed查验事实。"""
        policy_data = decode(await conn.fetchval("SELECT data FROM finance_compliance_plan WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["plan_id"]))
        max_age = policy_data["bindings"]["policy"]["verification_max_age_hours"]
        valid_until = min(datetime.fromisoformat(snapshot.valid_until), datetime.fromisoformat(snapshot.observed_at) + timedelta(hours=max_age))
        verification, verification_valid = snapshot.verification_status, valid_until
        evidence: dict[str, Any] = {"provider": row["provider"], "raw_response_ref": receipt_id, "verified_at": snapshot.observed_at, "valid_until": valid_until.isoformat(), "authority_revision": snapshot.authority_revision}
        if same_authority(state, row, generation) and snapshot.verification_status in {"pending", "not_checked", "unavailable"} and state["verification_status"] == "passed" and state["valid_until"] is not None and datetime.fromisoformat(state["valid_until"]) > datetime.now(UTC):
            verification, verification_valid = "passed", datetime.fromisoformat(state["valid_until"])
            old_evidence = state.get("verification_evidence")
            evidence = decode(old_evidence) if old_evidence is not None else evidence
        await conn.execute("UPDATE finance_invoice_compliance SET version=version+1,authority_revision=$4,verification_status=$5,invoice_status=$6,valid_until=$7,snapshot=$8::jsonb,verification_evidence=$9::jsonb,provider=$10,configuration_version=$11,provider_generation=$12 WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", row["tenant_id"], row["legal_entity_id"], invoice["id"], snapshot.authority_revision, verification, snapshot.invoice_status, verification_valid, snapshot.model_dump_json(), json.dumps(evidence), row["provider"], row["configuration_version"], generation)
        for case in snapshot.red_letter_cases:
            await self._f3_upsert_red_case(conn, row, invoice, case.model_dump(mode="json"), generation)
        for adjustment in snapshot.adjustments:
            data = adjustment.model_dump(mode="json")
            origin = await conn.fetchval("INSERT INTO finance_tax_adjustment(tenant_id,legal_entity_id,id,owner_id,invoice_id,external_adjustment_id,status,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT(tenant_id,legal_entity_id,external_adjustment_id) DO UPDATE SET version=finance_tax_adjustment.version+1,status=EXCLUDED.status,data=EXCLUDED.data WHERE finance_tax_adjustment.invoice_id=EXCLUDED.invoice_id RETURNING invoice_id", row["tenant_id"], row["legal_entity_id"], "f3adj_" + identifier(), invoice["owner_id"], invoice["id"], data["external_adjustment_id"], data["status"], json.dumps(data))
            if origin != invoice["id"]:
                raise FinanceError("TAX_ADJUSTMENT_SCOPE_MISMATCH", 409, "外部税务调整义务不能跨原蓝票覆盖")
        await self._event(conn, {**dict(invoice), "id": invoice["id"]}, "compliance.tax_snapshot", {"authority_revision": snapshot.authority_revision, "receipt_id": receipt_id})
        await conn.execute("UPDATE finance_archive_health h SET supplement_required=true FROM finance_archive a WHERE a.tenant_id=h.tenant_id AND a.legal_entity_id=h.legal_entity_id AND a.id=h.archive_id AND a.tenant_id=$1 AND a.legal_entity_id=$2 AND ((a.target_type='invoice' AND a.target_id=$3) OR EXISTS(SELECT 1 FROM finance_allocation al WHERE al.tenant_id=a.tenant_id AND al.legal_entity_id=a.legal_entity_id AND al.document_id=a.target_id AND al.invoice_id=$3))", row["tenant_id"], row["legal_entity_id"], invoice["id"])

    async def _f3_upsert_red_case(self, conn: Connection, row: Any, invoice: Any, data: dict[str, Any], generation: int) -> None:
        """原始initiated时间一经存在不得刷新，旧红字revision不能覆盖新事实。"""
        original = decode(invoice["data"])
        if data["blue_invoice_id"] != invoice["id"] or data["buyer_tax_id"] != original["buyer_tax_id"] or data["seller_tax_id"] != original["seller_tax_id"]:
            raise FinanceError("RED_CASE_SCOPE_MISMATCH", 409, "红字确认单与原蓝票主体不符")
        await validate_issued_balance(conn, invoice, data)
        old = await conn.fetchrow("SELECT * FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND external_case_id=$3 FOR UPDATE", row["tenant_id"], row["legal_entity_id"], data["external_case_id"])
        if old is not None:
            previous = decode(old["data"])
            if previous["initiated_at"] != data["initiated_at"] or old["invoice_id"] != invoice["id"]:
                raise FinanceError("RED_CASE_ORIGIN_CHANGED", 409, "权威红字确认单原始发起事实不可刷新")
            if old["status"] == "issued" and (data["status"], data["amount"], data["tax"]) != ("issued", previous["amount"], previous["tax"]):
                raise FinanceError("RED_ISSUED_FACT_CHANGED", 409, "已实际开具红票的金额和开具事实不可回退")
            if same_authority(dict(old), row, generation) and old["authority_revision"] >= data["authority_revision"]:
                return
        origin = await conn.fetchval("INSERT INTO finance_red_letter_case(tenant_id,legal_entity_id,id,owner_id,invoice_id,external_case_id,authority_revision,status,data,provider,configuration_version,provider_generation) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12) ON CONFLICT(tenant_id,legal_entity_id,external_case_id) DO UPDATE SET version=finance_red_letter_case.version+1,authority_revision=EXCLUDED.authority_revision,status=EXCLUDED.status,data=EXCLUDED.data,provider=EXCLUDED.provider,configuration_version=EXCLUDED.configuration_version,provider_generation=EXCLUDED.provider_generation WHERE finance_red_letter_case.invoice_id=EXCLUDED.invoice_id RETURNING invoice_id", row["tenant_id"], row["legal_entity_id"], "f3red_" + identifier(), invoice["owner_id"], invoice["id"], data["external_case_id"], data["authority_revision"], data["status"], json.dumps(data), row["provider"], row["configuration_version"], generation)
        if origin != invoice["id"]:
            raise FinanceError("RED_CASE_SCOPE_MISMATCH", 409, "外部红字确认号不能跨原蓝票并发覆盖")

    async def _f3_exception(self, conn: Connection, row: Any, code: str, evidence: dict[str, Any]) -> None:
        """未知、冲突和人工核对始终留下独立公司内审计异常。"""
        await conn.execute("INSERT INTO finance_compliance_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,rule_id,data) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)", row["tenant_id"], row["legal_entity_id"], identifier(), row["owner_id"], row["id"], code, json.dumps(evidence))

    async def _f3_commit_archive(self, conn: Connection, row: Any, outcome: ArchiveOutcome, request: dict[str, Any]) -> None:
        """保留原snapshot的真实封存事实；发送后新事实只要求补包。"""
        if row["action"] == "audit_archive":
            original_request = request["payload"]["archive_request"]
            self._f3_archive_scope(outcome, original_request)
            await conn.execute("INSERT INTO finance_archive_audit(tenant_id,legal_entity_id,id,owner_id,archive_id,operation_id,data) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)", row["tenant_id"], row["legal_entity_id"], identifier(), row["owner_id"], row["target_id"], row["id"], outcome.model_dump_json())
            await conn.execute("UPDATE finance_archive_health SET last_audit_status='passed' WHERE tenant_id=$1 AND legal_entity_id=$2 AND archive_id=$3", row["tenant_id"], row["legal_entity_id"], row["target_id"])
            return
        snapshot = request["payload"]["archive_snapshot"]
        self._f3_archive_scope(outcome, request)
        archive_id = "f3archive_" + identifier()
        await conn.execute("INSERT INTO finance_archive(tenant_id,legal_entity_id,id,owner_id,target_type,target_id,operation_id,manifest_hash,source_snapshot_hash,parent_manifest_hash,snapshot,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb)", row["tenant_id"], row["legal_entity_id"], archive_id, row["owner_id"], row["target_type"], row["target_id"], row["id"], outcome.manifest_hash, outcome.source_snapshot_hash, outcome.parent_manifest_hash, json.dumps(snapshot), outcome.model_dump_json())
        live_version = await self._f3_live_compliance_version(conn, row)
        archive = {"tenant_id": row["tenant_id"], "legal_entity_id": row["legal_entity_id"], "id": archive_id, "target_type": row["target_type"], "target_id": row["target_id"], "snapshot": snapshot}
        supplement = live_version != snapshot["compliance_version"] or bool(await archive_source_changes(conn, archive))
        await conn.execute("INSERT INTO finance_archive_health VALUES($1,$2,$3,$4,NULL)", row["tenant_id"], row["legal_entity_id"], archive_id, supplement)
        if outcome.parent_manifest_hash is not None:
            parent = await conn.fetchrow("SELECT tenant_id,legal_entity_id,id,target_type,target_id,snapshot FROM finance_archive WHERE tenant_id=$1 AND legal_entity_id=$2 AND manifest_hash=$3", row["tenant_id"], row["legal_entity_id"], outcome.parent_manifest_hash)
            if parent is None:
                await self._f3_exception(conn, row, "ARCHIVE_PARENT_MISSING", {"archive_id": archive_id, "parent_manifest_hash": outcome.parent_manifest_hash})
                return
            params = request["payload"]["parameters"]
            for event_id in params["source_event_ids"]:
                await conn.execute("INSERT INTO finance_archive_supplement(tenant_id,legal_entity_id,id,owner_id,archive_id,child_archive_id,source_event_id) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(tenant_id,legal_entity_id,archive_id,source_event_id) DO NOTHING", row["tenant_id"], row["legal_entity_id"], identifier(), row["owner_id"], parent["id"], archive_id, event_id)
            remaining = bool(await archive_source_changes(conn, parent))
            await conn.execute("UPDATE finance_archive_health SET supplement_required=$4 WHERE tenant_id=$1 AND legal_entity_id=$2 AND archive_id=$3", row["tenant_id"], row["legal_entity_id"], parent["id"], remaining)

    async def _f3_live_compliance_version(self, conn: Connection, row: Any) -> int:
        """封存提交只比较实时版本，不能据此拒绝原封存事实。"""
        if row["target_type"] == "invoice":
            value = await conn.fetchval("SELECT version FROM finance_invoice_compliance WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", row["tenant_id"], row["legal_entity_id"], row["target_id"])
        else:
            value = await conn.fetchval("SELECT max(c.version) FROM finance_invoice_compliance c JOIN finance_allocation a ON(a.tenant_id=c.tenant_id AND a.legal_entity_id=c.legal_entity_id AND a.invoice_id=c.invoice_id) WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.document_id=$3", row["tenant_id"], row["legal_entity_id"], row["target_id"])
        return int(value) if value is not None else 1

    def _f3_archive_scope(self, outcome: ArchiveOutcome, request: dict[str, Any]) -> None:
        """领域再次核对冻结清册、来源摘要、期限和每一对象版本锁。"""
        validate_archive_outcome(outcome, request)
