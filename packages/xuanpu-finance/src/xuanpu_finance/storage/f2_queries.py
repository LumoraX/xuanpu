"""应付/报销外部结果、固定计划及实际人工待办的权限查询。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionParameters, ActionPreview
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_base import money
from xuanpu_finance.storage.common import decode, encode_cursor, page, timestamp_cursor
from xuanpu_finance.storage.f2_execution import SettlementExecutionStore
from xuanpu_finance.storage.f2_plans import EXTERNAL_ACTIONS
from xuanpu_finance.storage.imports import ImportStore
from xuanpu_finance.storage.queries import ReadStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class SettlementQueryStore(SettlementExecutionStore, ImportStore, ReadStore):
    """外部明细与聚合结果来自同一财务库，不能凭前端猜成功。"""

    async def list_review_queue(
        self, who: FinancePrincipal, entity: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """业务单据和原行动计划共用同一持久、实际分派的审核待办。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, "review_queue"])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            if role not in {"approver", "reviewer"}:
                return page([], 0, None)
            joins = " FROM finance_workflow_step s JOIN finance_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id) JOIN finance_expense_claim c ON(c.tenant_id=w.tenant_id AND c.legal_entity_id=w.legal_entity_id AND c.id=w.document_id) WHERE s.tenant_id=$1 AND s.legal_entity_id=$2 AND s.approver_user_id=$3 AND s.status='pending' AND w.status='under_review'"
            total = await conn.fetchval(
                "SELECT count(*)" + joins, who.tenant_id, entity, who.user_id
            )
            rows = await conn.fetch(
                "SELECT c.*,s.id AS step_id,s.version AS step_version,s.approver_user_id,w.id AS workflow_id,w.version AS workflow_version,w.due_at,w.target_type,w.target_id AS workflow_target_id"
                + joins
                + " AND ($4::timestamptz IS NULL OR (c.created_at,s.id)>($4,$5::text)) ORDER BY c.created_at,s.id LIMIT $6",
                who.tenant_id,
                entity,
                who.user_id,
                stamp,
                last,
                limit + 1,
            )
            items = []
            for row in rows[:limit]:
                if row["target_type"] != "action_plan":
                    items.append(await self._review_item(conn, who, row))
                    continue
                original = await self._plan_row(conn, who, row["workflow_target_id"])
                payload = decode(original["data"])
                body = ActionPreview(
                    action="approve",
                    target_type="action_plan",
                    target_id=original["id"],
                    expected_version=1,
                    parameters=ActionParameters(workflow_step_id=row["step_id"]),
                )
                blockers = await self._check_plan_review(conn, who, original, body)
                items.append(
                    {
                        "workflow_step_id": row["step_id"],
                        "target_id": original["id"],
                        "target_type": "action_plan",
                        "target_version": 1,
                        "summary": {
                            "execute_payment": "付款",
                            "submit_posting": "记账",
                            "reverse_posting": "冲回记账",
                        }[payload["action"]]
                        + " "
                        + payload["parameters"]["amount"]
                        + " 元",
                        "total": payload["parameters"]["amount"],
                        "evidence": await self._claim_evidence(conn, row),
                        "workflow_id": row["workflow_id"],
                        "workflow_version": row["workflow_version"],
                        "step_version": row["step_version"],
                        "approver_user_id": row["approver_user_id"],
                        "status": "pending",
                        "due_at": row["due_at"].isoformat(),
                        "blockers": blockers,
                    }
                )
            return page(
                items,
                total,
                encode_cursor(
                    scope, [rows[limit - 1]["created_at"].isoformat(), rows[limit - 1]["step_id"]]
                )
                if len(rows) > limit
                else None,
            )

    async def list_action_plans(
        self,
        who: FinancePrincipal,
        entity: str,
        target_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """只列原外部行动计划，申请经办人和实际审核人可恢复同一实例。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, "plans", target_id])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            where = "p.tenant_id=$1 AND p.legal_entity_id=$2 AND p.data->>'action'=ANY($3::text[]) AND ($4::text IS NULL OR p.target_id=$4 OR p.data->>'target_id'=$4) AND (p.owner_id=$5 OR $6::text='reviewer' OR ($6::text='approver' AND EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE w.tenant_id=p.tenant_id AND w.legal_entity_id=p.legal_entity_id AND w.target_type='action_plan' AND w.target_id=p.id AND s.approver_user_id=$5)))"
            args = (who.tenant_id, entity, list(EXTERNAL_ACTIONS), target_id, who.user_id, role)
            total = await conn.fetchval(
                "SELECT count(*) FROM finance_action_plan p WHERE " + where, *args
            )
            rows = await conn.fetch(
                "SELECT p.* FROM finance_action_plan p WHERE "
                + where
                + " AND ($7::timestamptz IS NULL OR (p.expires_at,p.id)>($7,$8::text)) ORDER BY p.expires_at,p.id LIMIT $9",
                *args,
                stamp,
                last,
                limit + 1,
            )
            return page(
                [await self._plan_view(conn, who, row) for row in rows[:limit]],
                total,
                encode_cursor(
                    scope, [rows[limit - 1]["expires_at"].isoformat(), rows[limit - 1]["id"]]
                )
                if len(rows) > limit
                else None,
            )

    async def get_payment(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """先验证父单据权限，再返回脱敏付款结果。"""
        return await self._get_settlement_record(who, id, "finance_payment")

    async def get_posting(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """凭证及冲回记录可恢复原始关联，不改写历史。"""
        return await self._get_settlement_record(who, id, "finance_posting")

    async def _get_settlement_record(
        self, who: FinancePrincipal, id: str, table: str
    ) -> dict[str, Any]:
        """表名只来自内部固定调用，不接收客户端SQL片段。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
            )
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "外部业务记录不存在")
            doc = await self._claim_row(conn, who, row["document_id"])
            return await self._settlement_record_view(conn, row, doc)

    async def _settlement_record_view(self, conn: Connection, row: Any, doc: Any) -> dict[str, Any]:
        """公开记录不包含渠道账户令牌或供应商凭据。"""
        result = {
            k: row[k]
            for k in (
                "id",
                "version",
                "legal_entity_id",
                "document_id",
                "operation_id",
                "status",
                "simulation",
            )
        }
        result.update(
            {
                "document_kind": doc["document_kind"],
                "amount": money(row["amount"]),
                "created_at": row["created_at"].isoformat(),
                **decode(row["data"]),
            }
        )
        if "original_posting_id" in row:
            result.update(
                {
                    "kind": "reversal" if row["original_posting_id"] else "posting",
                    "original_posting_id": row["original_posting_id"],
                }
            )
            amounts = await conn.fetchrow(
                "SELECT COALESCE(sum(amount) FILTER(WHERE status='succeeded'),0) AS succeeded,COALESCE(sum(amount) FILTER(WHERE status IN ('accepted','running','dispatching','awaiting_provider','outcome_unknown')),0) AS pending FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND target_id=$3 AND action='reverse_posting'",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
            )
            assert amounts is not None
            available = (
                row["amount"] - amounts["succeeded"] - amounts["pending"]
                if row["status"] == "succeeded" and row["original_posting_id"] is None
                else 0
            )
            result.update(
                {
                    "reversal_available_amount": money(available),
                    "reversal_pending_amount": money(amounts["pending"]),
                }
            )
        return result

    async def list_payments(
        self,
        who: FinancePrincipal,
        entity: str,
        document_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """付款明细仅显示授权单据，total独立于分页。"""
        return await self._list_settlement_records(
            who, entity, "finance_payment", document_id, cursor, limit
        )

    async def list_postings(
        self,
        who: FinancePrincipal,
        entity: str,
        document_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """入账与冲回明细分别保留，不能覆盖原凭证。"""
        return await self._list_settlement_records(
            who, entity, "finance_posting", document_id, cursor, limit
        )

    async def _list_settlement_records(
        self,
        who: FinancePrincipal,
        entity: str,
        table: str,
        document_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """权限过滤后计数，按创建时间和ID稳定分页。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, table, document_id])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            if document_id is not None:
                await self._claim_row(conn, who, document_id)
            where = "r.tenant_id=$1 AND r.legal_entity_id=$2 AND ($3::text IS NULL OR r.document_id=$3) AND (c.owner_id=$4 OR $5::text='reviewer' OR ($5::text='approver' AND EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE w.tenant_id=c.tenant_id AND w.legal_entity_id=c.legal_entity_id AND w.document_id=c.id AND s.approver_user_id=$4" + self._legacy_assignment_visibility_sql() + ")))"
            joins = f" FROM {table} r JOIN finance_expense_claim c ON(c.tenant_id=r.tenant_id AND c.legal_entity_id=r.legal_entity_id AND c.id=r.document_id) WHERE "
            args = (who.tenant_id, entity, document_id, who.user_id, role)
            total = await conn.fetchval("SELECT count(*)" + joins + where, *args)
            rows = await conn.fetch(
                "SELECT r.*"
                + joins
                + where
                + " AND ($6::timestamptz IS NULL OR (r.created_at,r.id)>($6,$7::text)) ORDER BY r.created_at,r.id LIMIT $8",
                *args,
                stamp,
                last,
                limit + 1,
            )
            items = []
            for row in rows[:limit]:
                items.append(
                    await self._settlement_record_view(
                        conn, row, await self._claim_row(conn, who, row["document_id"])
                    )
                )
            return page(
                items,
                total,
                encode_cursor(
                    scope, [rows[limit - 1]["created_at"].isoformat(), rows[limit - 1]["id"]]
                )
                if len(rows) > limit
                else None,
            )

    async def get_operation(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """F2外部Operation与F0导入、F1本地业务操作分开恢复。"""
        if not id.startswith("f2op_"):
            return await super().get_operation(who, id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM finance_settlement_operation WHERE tenant_id=$1 AND id=$2",
                who.tenant_id,
                id,
            )
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "操作不存在")
            await self._claim_row(conn, who, row["document_id"])
            return self._settlement_operation_view(row)

    async def cancel_operation(
        self, who: FinancePrincipal, id: str, expected_version: int, key: str
    ) -> dict[str, Any]:
        """未发送可取消，发送后的取消仅进入未知并核对，绝不释放金额。"""
        if not id.startswith("f2op_"):
            return await super().cancel_operation(who, id, expected_version, key)
        return await self._control_settlement(who, id, expected_version, key, reconcile=False)

    async def reconcile_operation(
        self, who: FinancePrincipal, id: str, expected_version: int, key: str
    ) -> dict[str, Any]:
        """人工只触发原请求号只读回查，没有重发付款路径。"""
        if not id.startswith("f2op_"):
            raise FinanceError("RECONCILE_NOT_APPLICABLE", 422, "仅外部财务操作可核对")
        return await self._control_settlement(who, id, expected_version, key, reconcile=True)

    async def _control_settlement(
        self, who: FinancePrincipal, id: str, version: int, key: str, *, reconcile: bool
    ) -> dict[str, Any]:
        """领域再次要求真实人工和CAS，取消竞态以单据/操作锁串行。"""
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "取消及人工核对必须真人明确决定")
        request = {"id": id, "expected_version": version}
        operation = "reconcileOperation" if reconcile else "cancelOperation"
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM finance_settlement_operation WHERE tenant_id=$1 AND id=$2",
                who.tenant_id,
                id,
            )
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "操作不存在")
            doc = await self._claim_row(conn, who, row["document_id"], lock=True)
            if (
                row["owner_id"] != who.user_id
                and await self._role(conn, who, row["legal_entity_id"]) != "reviewer"
            ):
                raise FinanceError("EXECUTOR_FORBIDDEN", 403, "仅经办或当前财务reviewer可核对")
            old = await self._business_previous(
                conn, who, row["legal_entity_id"], operation, key, request
            )
            if old is not None:
                return old
            row = await conn.fetchrow(
                "SELECT * FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE",
                who.tenant_id,
                row["legal_entity_id"],
                id,
            )
            assert row is not None
            if row["version"] != version:
                raise FinanceError("VERSION_CONFLICT", 409, "操作版本已变化")
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                raise FinanceError("OPERATION_TERMINAL", 409, "已确认操作不能取消或直接改写")
            if reconcile and row["request"] is None:
                raise FinanceError("RECONCILE_NOT_SENT", 409, "尚未发送不能查单")
            status = "outcome_unknown" if row["request"] is not None else "cancelled"
            row = await conn.fetchrow(
                "UPDATE finance_settlement_operation SET status=$4,version=version+1,query_requested=$5,next_query_at=CASE WHEN $5 THEN now() ELSE NULL END,lease_token=NULL,lease_until=NULL WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                who.tenant_id,
                row["legal_entity_id"],
                id,
                status,
                status == "outcome_unknown",
            )
            assert row is not None
            await self._update_record_state(conn, row, status, {})
            result = self._settlement_operation_view(row)
            await self._event(
                conn,
                doc,
                "settlement." + ("reconcile_requested" if reconcile else "cancel_requested"),
                result,
            )
            await self._settlement_outbox(conn, row, result)
            return await self._business_remember(
                conn, who, row["legal_entity_id"], operation, key, request, result
            )
