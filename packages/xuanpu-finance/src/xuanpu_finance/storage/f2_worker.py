"""F2持久租约队列和固定发送边界，发送后恢复只允许原请求查单。"""

from __future__ import annotations

import json
from typing import Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f2_results import SettlementResultStore


class SettlementWorkerStore(SettlementResultStore):
    """所有网络副作用在短事务之外，数据库固定请求并提供fencing。"""

    async def claim_settlement(self, *, allow_dispatch: bool = True) -> dict[str, Any] | None:
        """未发送可恢复submit，任何已发送/崩溃操作始终只给query。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM finance_settlement_operation WHERE (lease_until IS NULL OR lease_until<now()) AND ((status IN ('accepted','running') AND $1) OR (status='dispatching') OR (status IN ('awaiting_provider','outcome_unknown') AND (query_requested OR next_query_at<=now()))) ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED",
                allow_dispatch,
            )
            if row is None:
                return None
            query = row["request"] is not None
            status = (
                "outcome_unknown"
                if row["status"] == "dispatching"
                else row["status"]
                if query
                else "running"
            )
            row = await conn.fetchrow(
                "UPDATE finance_settlement_operation SET status=$2,version=version+1,lease_token=$3,lease_until=now()+interval '60 seconds',query_requested=false WHERE id=$1 RETURNING *",
                row["id"],
                status,
                identifier(),
            )
            assert row is not None
            await conn.execute(
                "INSERT INTO finance_settlement_lease(lease_token,tenant_id,legal_entity_id,operation_id) VALUES($1,$2,$3,$4)",
                row["lease_token"],
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
            )
            plan = await conn.fetchrow(
                "SELECT * FROM finance_action_plan WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                row["tenant_id"],
                row["legal_entity_id"],
                row["plan_id"],
            )
            assert plan is not None
            payload = decode(plan["data"])
            users = {row["owner_id"]}
            if not query:
                workflows = await conn.fetch(
                    "SELECT w.id FROM finance_workflow w WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.document_id=$3 AND ((w.target_type<>'action_plan' AND w.status='approved') OR (w.target_type='action_plan' AND w.target_id=$4 AND w.status='approved'))",
                    row["tenant_id"],
                    row["legal_entity_id"],
                    row["document_id"],
                    row["plan_id"],
                )
                for workflow in workflows:
                    decisions = await conn.fetch(
                        "SELECT actor_id FROM finance_decision WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 AND action='approve'",
                        row["tenant_id"],
                        row["legal_entity_id"],
                        workflow["id"],
                    )
                    users.update(d["actor_id"] for d in decisions)
            return {
                "id": row["id"],
                "tenant_id": row["tenant_id"],
                "legal_entity_id": row["legal_entity_id"],
                "lease_token": row["lease_token"],
                "mode": "query" if query else "submit",
                "action": row["action"],
                "request": decode(row["request"]) if query else None,
                "actor_id": row["owner_id"],
                "provider": row["provider"],
                "configuration_version": row["configuration_version"],
                "authorization_user_ids": sorted(users),
                "simulation": payload["simulation"],
            }

    async def renew_settlement(self, job: dict[str, Any]) -> None:
        """只续租仍有效的同一fencing令牌，丢失立即拒绝后续写入。"""
        result = await self.pool.execute(
            "UPDATE finance_settlement_operation SET lease_until=now()+interval '60 seconds' WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND lease_token=$4 AND lease_until>now() AND status IN ('running','dispatching','awaiting_provider','outcome_unknown')",
            job["tenant_id"],
            job["legal_entity_id"],
            job["id"],
            job["lease_token"],
        )
        if result != "UPDATE 1":
            raise FinanceError("LEASE_LOST", 409, "外部操作租约已丢失")

    async def prepare_settlement_dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        """发送前在线重审授权并固定请求摘要；从此不再允许submit重试。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row, doc = await self._worker_lock(conn, job)
            if row["request"] is not None:
                raise FinanceError(
                    "ALREADY_DISPATCHED", 409, "请求已经固定发送，只能按原请求号核对"
                )
            if row["status"] != "running" or job["mode"] != "submit":
                raise FinanceError("DISPATCH_NOT_ALLOWED", 409, "任务当前不可新发送")
            who = FinancePrincipal(row["tenant_id"], row["owner_id"], origin="human")
            plan = await self._plan_row(conn, who, row["plan_id"])
            blockers = await self._external_plan_blockers(
                conn, who, plan, lock=True, executing_id=row["id"]
            )
            if blockers:
                self._raise_blocker(blockers)
            assert plan is not None
            request = self._dispatch_request(row, plan)
            await conn.execute(
                "UPDATE finance_settlement_operation SET status='dispatching',version=version+1,request=$4::jsonb WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                json.dumps(request),
            )
            await self._event(
                conn,
                doc,
                "settlement.dispatching",
                {
                    "operation_id": row["id"],
                    "external_request_id": row["external_request_id"],
                    "payload_hash": request["payload_hash"],
                },
            )
            job["request"] = request
            return request

    def _dispatch_request(self, row: Any, plan: Any) -> dict[str, Any]:
        """仅从已批准固定计划构造HTTP请求，摘要不含自身。"""
        payload = decode(plan["data"])
        bindings = payload["bindings"]
        request = {
            "protocol_version": "1",
            "tenant_id": row["tenant_id"],
            "legal_entity_id": row["legal_entity_id"],
            "operation_id": row["id"],
            "action": row["action"],
            "external_request_id": row["external_request_id"],
            "provider": row["provider"],
            "adapter_version": row["adapter_version"],
            "configuration_version": row["configuration_version"],
            "simulation": row["simulation"],
            "amount": payload["parameters"]["amount"],
            "currency": "CNY",
            "target": {
                "id": payload["target_id"],
                "type": payload["target_type"],
                "version": payload["target_version"],
            },
            "plan_digest": plan["digest"],
        }
        if row["action"] == "execute_payment":
            request.update(
                {
                    "payee_ref": bindings["payee"]["ref"],
                    "provider_account_id": bindings["payee"]["provider_account_id"],
                }
            )
        else:
            request.update(
                {
                    "ledger_id": bindings["ledger_id"],
                    "accounting_period": bindings["accounting_period"],
                }
            )
            if row["action"] == "reverse_posting":
                request.update(
                    {
                        "original_voucher_id": bindings["original_posting"]["data"]["voucher_id"],
                        "reason": payload["parameters"]["reason"],
                    }
                )
        request["payload_hash"] = request_digest(request)
        return request

    async def reject_settlement_before_dispatch(self, job: dict[str, Any], code: str) -> None:
        """已确定未发送的授权失败可释放该动作占额。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row, doc = await self._worker_lock(conn, job)
            if row["request"] is not None:
                raise FinanceError("ALREADY_DISPATCHED", 409, "已发送不得按发送前失败释放")
            row = await conn.fetchrow(
                "UPDATE finance_settlement_operation SET status='failed',version=version+1,last_error=$4 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                code,
            )
            assert row is not None
            await self._update_record_state(conn, row, "failed", {})
            await self._event(
                conn,
                doc,
                "settlement.failed_before_dispatch",
                {"operation_id": row["id"], "code": code},
            )
            await self._settlement_outbox(conn, row, self._settlement_operation_view(row))

    async def defer_settlement(self, job: dict[str, Any], code: str, *, uncertain: bool) -> None:
        """超时/取消/失联均保留占额，最多四次定时查单后转人工。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row, doc = await self._worker_lock(conn, job)
            if row["request"] is None:
                if uncertain:
                    raise FinanceError("REQUEST_NOT_PREPARED", 409, "未知结果必须关联固定外部请求")
                status = "running"
            else:
                status = "outcome_unknown" if uncertain else "awaiting_provider"
            count = row["query_count"] + (1 if job["mode"] == "query" else 0)
            delays = (60, 300, 900, 3600)
            delay = delays[min(count, 3)]
            row = await conn.fetchrow(
                "UPDATE finance_settlement_operation SET status=$4,version=version+1,last_error=$5,query_count=$6,next_query_at=CASE WHEN $6<4 THEN now()+($7::integer*interval '1 second') ELSE NULL END WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                status,
                code,
                count,
                delay,
            )
            assert row is not None
            await self._update_record_state(conn, row, status, {})
            await self._event(
                conn, doc, "settlement." + status, {"operation_id": row["id"], "code": code}
            )
            if count >= 4:
                await self._settlement_exception(
                    conn, row, "MANUAL_RECONCILIATION_REQUIRED", {"code": code}
                )
            await self._settlement_outbox(conn, row, self._settlement_operation_view(row))

    async def release_settlement(self, job: dict[str, Any]) -> None:
        """只清理本worker租约，旧实例不能释放新持有者。"""
        await self.pool.execute(
            "UPDATE finance_settlement_operation SET lease_token=NULL,lease_until=NULL WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND lease_token=$4",
            job["tenant_id"],
            job["legal_entity_id"],
            job["id"],
            job["lease_token"],
        )
