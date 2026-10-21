"""渠道原始回执、租约核验与付款/凭证事实的同事务提交。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f2_queries import SettlementQueryStore

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection

RESULT_SCOPE = (
    "protocol_version",
    "tenant_id",
    "legal_entity_id",
    "operation_id",
    "action",
    "external_request_id",
    "provider",
    "adapter_version",
    "configuration_version",
    "payload_hash",
    "simulation",
    "amount",
    "currency",
    "target",
    "plan_digest",
)


class SettlementResultStore(SettlementQueryStore):
    """回执原字节先保存，业务结果只有精确归属与凭证成立才提交。"""

    async def _worker_lock(self, conn: Connection, job: dict[str, Any]) -> tuple[Any, Any]:
        """先父单据后Operation，和人工取消/执行使用同一锁序。"""
        current = await conn.fetchrow(
            "SELECT * FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            job["tenant_id"],
            job["legal_entity_id"],
            job["id"],
        )
        if current is None:
            raise FinanceError("LEASE_LOST", 409, "外部操作不存在")
        doc = await conn.fetchrow(
            "SELECT * FROM finance_expense_claim WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE",
            job["tenant_id"],
            job["legal_entity_id"],
            current["document_id"],
        )
        row = await conn.fetchrow(
            "SELECT *,lease_until>now() AS lease_active FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE",
            job["tenant_id"],
            job["legal_entity_id"],
            job["id"],
        )
        if row is None:
            raise FinanceError("LEASE_LOST", 409, "外部操作已不存在")
        if (
            row["lease_token"] != job["lease_token"]
            or not row["lease_active"]
            or row["status"] in {"succeeded", "failed", "cancelled"}
        ):
            raise FinanceError("LEASE_LOST", 409, "外部操作fencing令牌已过期或失效")
        return row, doc

    async def record_settlement_result(
        self, job: dict[str, Any], result: dict[str, Any], raw: bytes
    ) -> None:
        """过期worker只能追加标记为stale的原始证据，不能推进资金状态。"""
        if len(raw) > 1048576:
            raise FinanceError("PROVIDER_RESPONSE_TOO_LARGE", 422, "渠道回执超出保全上限")
        try:
            await self._record_settlement_result_fenced(job, result, raw)
        except FinanceError as exc:
            if exc.code != "LEASE_LOST":
                raise
            async with self.pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                    job["tenant_id"],
                    job["legal_entity_id"],
                    job["id"],
                )
                if row is None or row["request"] is None:
                    raise
                valid_lease = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM finance_settlement_lease WHERE lease_token=$1 AND tenant_id=$2 AND legal_entity_id=$3 AND operation_id=$4)",
                    job["lease_token"],
                    row["tenant_id"],
                    row["legal_entity_id"],
                    row["id"],
                )
                if not valid_lease:
                    raise FinanceError(
                        "STALE_RECEIPT_SCOPE_INVALID", 409, "过期租约不属于该外部操作"
                    ) from exc
                if any(
                    job.get(k) != row[k] for k in ("provider", "configuration_version", "action")
                ):
                    raise FinanceError(
                        "STALE_RECEIPT_SCOPE_INVALID", 409, "过期回执不能关联其它请求"
                    ) from exc
                await conn.execute(
                    "INSERT INTO finance_settlement_receipt(tenant_id,legal_entity_id,id,operation_id,raw,sha256,data) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
                    row["tenant_id"],
                    row["legal_entity_id"],
                    identifier(),
                    row["id"],
                    raw,
                    hashlib.sha256(raw).hexdigest(),
                    json.dumps({"stale": True, "validation_error": "LEASE_LOST", "result": result}),
                )
            raise

    async def _record_settlement_result_fenced(
        self, job: dict[str, Any], result: dict[str, Any], raw: bytes
    ) -> None:
        """错误归属/金额或缺证据仅保全为未知，绝不能成为成功。"""
        if len(raw) > 1048576:
            raise FinanceError("PROVIDER_RESPONSE_TOO_LARGE", 422, "渠道回执超出保全上限")
        async with self.pool.acquire() as conn, conn.transaction():
            row, doc = await self._worker_lock(conn, job)
            if row["request"] is None:
                raise FinanceError("REQUEST_NOT_PREPARED", 409, "没有固定请求不能应用渠道回执")
            request = decode(row["request"])
            error = self._receipt_error(request, result)
            if error is None and result["status"] == "succeeded":
                error = await self._duplicate_provider_fact(conn, row, result)
            await conn.execute(
                "INSERT INTO finance_settlement_receipt(tenant_id,legal_entity_id,id,operation_id,raw,sha256,data) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
                row["tenant_id"],
                row["legal_entity_id"],
                identifier(),
                row["id"],
                raw,
                hashlib.sha256(raw).hexdigest(),
                json.dumps(result),
            )
            state = {
                "accepted": "awaiting_provider",
                "pending": "awaiting_provider",
                "succeeded": "succeeded",
                "failed": "failed",
                "unknown": "outcome_unknown",
                "not_found": "outcome_unknown",
            }.get(str(result.get("status")), "outcome_unknown")
            if error:
                state = "outcome_unknown"
            count = row["query_count"] + (1 if job["mode"] == "query" else 0)
            delay = (60, 300, 900, 3600)[min(count, 3)]
            row = await conn.fetchrow(
                "UPDATE finance_settlement_operation SET status=$4,version=version+1,result=$5::jsonb,last_error=$6,query_count=$7,next_query_at=CASE WHEN $4 IN ('awaiting_provider','outcome_unknown') AND $7<4 THEN now()+($8::integer*interval '1 second') ELSE NULL END WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                state,
                json.dumps(result),
                error,
                count,
                delay,
            )
            assert row is not None
            await self._update_record_state(conn, row, state, result if not error else {})
            await self._event(
                conn,
                doc,
                "settlement." + state,
                {
                    "operation_id": row["id"],
                    "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                    "simulation": row["simulation"],
                    "validation_error": error,
                },
            )
            if error:
                await self._settlement_exception(
                    conn, row, error, {"receipt_sha256": hashlib.sha256(raw).hexdigest()}
                )
            elif state in {"outcome_unknown", "awaiting_provider"} and count >= 4:
                await self._settlement_exception(
                    conn, row, "MANUAL_RECONCILIATION_REQUIRED", {"query_count": count}
                )
            await self._settlement_outbox(conn, row, self._settlement_operation_view(row))
            if state == "succeeded":
                await self._consume_completed_allocations(conn, doc)

    async def _duplicate_provider_fact(
        self, conn: Connection, row: Any, result: dict[str, Any]
    ) -> str | None:
        """相同渠道资金交易或账套期间凭证只能计入一个真实外部事实。"""
        payment = row["action"] == "execute_payment"
        table = "finance_payment" if payment else "finance_posting"
        field = "transaction_id" if payment else "voucher_id"
        ledger = "" if payment else result["ledger_id"]
        period = "" if payment else result["accounting_period"]
        scope = [
            row["tenant_id"],
            row["legal_entity_id"],
            row["provider"],
            row["configuration_version"],
            field,
            result[field],
            ledger,
            period,
        ]
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", request_digest(scope)
        )
        duplicate = await conn.fetchval(
            f"SELECT EXISTS(SELECT 1 FROM {table} r JOIN finance_settlement_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id) WHERE o.tenant_id=$1 AND o.legal_entity_id=$2 AND o.provider=$3 AND o.configuration_version=$4 AND o.id<>$5 AND r.status='succeeded' AND r.data->>$6=$7 AND ($8='' OR (r.data->>'ledger_id'=$8 AND r.data->>'accounting_period'=$9)))",
            row["tenant_id"],
            row["legal_entity_id"],
            row["provider"],
            row["configuration_version"],
            row["id"],
            field,
            result[field],
            ledger,
            period,
        )
        return (
            "DUPLICATE_EXTERNAL_TRANSACTION"
            if duplicate and payment
            else "DUPLICATE_LEDGER_VOUCHER"
            if duplicate
            else None
        )

    def _receipt_error(self, request: dict[str, Any], result: dict[str, Any]) -> str | None:
        """存储层复核跨字段归属和成功证据，不能只依赖HTTP适配器。"""
        if result.get("validation_error"):
            return str(result["validation_error"])
        if any(result.get(k) != request[k] for k in RESULT_SCOPE):
            return "PROVIDER_RESPONSE_SCOPE_MISMATCH"
        if result.get("status") not in {
            "accepted",
            "pending",
            "succeeded",
            "failed",
            "unknown",
            "not_found",
        }:
            return "PROVIDER_RESPONSE_INVALID"
        if result["status"] != "succeeded":
            return None
        error = self._evidence_error(result.get("evidence"))
        if error:
            return error
        if request["action"] == "execute_payment":
            if not result.get("external_order_id") or not result.get("transaction_id"):
                return "PAYMENT_TRANSACTION_REQUIRED"
        elif (
            not result.get("voucher_id")
            or result.get("ledger_id") != request["ledger_id"]
            or result.get("accounting_period") != request["accounting_period"]
        ):
            return "POSTING_VOUCHER_REQUIRED"
        return None

    def _evidence_error(self, evidence: Any) -> str | None:
        """成功回执须提供完整非空的渠道证据引用和规范摘要。"""
        if not isinstance(evidence, list) or not evidence:
            return "PROVIDER_EVIDENCE_REQUIRED"
        for item in evidence:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str) and item[k]
                for k in ("id", "sha256", "media_type", "object_ref")
            ):
                return "PROVIDER_EVIDENCE_INVALID"
            if len(item["sha256"]) != 64 or any(
                c not in "0123456789abcdef" for c in item["sha256"]
            ):
                return "PROVIDER_EVIDENCE_INVALID"
        return None

    async def _consume_completed_allocations(self, conn: Connection, doc: Any) -> None:
        """资金或记账整单完成后保留consumed，部分结果不能核销整票。"""
        totals = await self._settlement_totals(conn, doc)
        if totals["paid_amount"] != format(doc["total"], ".2f") and totals[
            "posted_amount"
        ] != format(doc["total"], ".2f"):
            return
        await self._lock_claim_invoices(conn, doc)
        await conn.execute(
            "UPDATE finance_allocation SET state='consumed',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='reserved'",
            doc["tenant_id"],
            doc["legal_entity_id"],
            doc["id"],
        )

    async def _settlement_exception(
        self, conn: Connection, row: Any, rule: str, evidence: dict[str, Any]
    ) -> None:
        """外部核对异常不伪造F0导入item外键，也不允许人工改成已付。"""
        await conn.execute(
            "INSERT INTO finance_settlement_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,rule_id,evidence) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
            row["tenant_id"],
            row["legal_entity_id"],
            identifier(),
            row["owner_id"],
            row["id"],
            rule,
            json.dumps(evidence),
        )
