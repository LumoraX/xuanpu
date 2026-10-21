"""业务命令的同事务幂等、历史和最小授权基础。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import FinanceBase, decode

if TYPE_CHECKING:
    import asyncpg

    from xuanpu_finance.models import FinancePrincipal

    Connection = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy


def money(value: Any) -> str:
    """精确格式化已验证或数据库金额。"""
    return format(Decimal(value), ".2f")


class BusinessBase(FinanceBase):
    """所有报销写命令只在注入的独立PG事务内完成。"""

    async def _business_previous(
        self,
        conn: Connection,
        who: FinancePrincipal,
        entity: str,
        operation: str,
        key: str,
        body: object,
    ) -> dict[str, Any] | None:
        """串行同一完整业务幂等范围，并拒绝同键异内容。"""
        if not key or len(key) > 128:
            raise FinanceError("IDEMPOTENCY_KEY_INVALID", 422, "幂等键长度无效")
        await self._role(conn, who, entity)
        scope = [who.tenant_id, who.user_id, entity, operation, key]
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", request_digest(scope)
        )
        row = await conn.fetchrow(
            "SELECT * FROM finance_business_idempotency WHERE tenant_id=$1 AND legal_entity_id=$2 AND actor_id=$3 AND operation_id=$4 AND key=$5",
            *scope[:1],
            entity,
            who.user_id,
            operation,
            key,
        )
        if row is None:
            return None
        if row["digest"] != request_digest(body):
            raise FinanceError("IDEMPOTENCY_CONFLICT", 409, "同幂等键请求内容不同")
        return decode(row["response"])

    async def _business_remember(
        self,
        conn: Connection,
        who: FinancePrincipal,
        entity: str,
        operation: str,
        key: str,
        body: object,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        """在业务状态相同事务中保留重试回执。"""
        await conn.execute(
            "INSERT INTO finance_business_idempotency VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
            who.tenant_id,
            entity,
            who.user_id,
            operation,
            key,
            request_digest(body),
            json.dumps(response),
        )
        return response

    async def _claim_row(
        self, conn: Connection, who: FinancePrincipal, id: str, *, lock: bool = False
    ) -> Any:
        """只让本人、公司reviewer或实际分派人员读取单据。"""
        query = "SELECT * FROM finance_expense_claim WHERE tenant_id=$1 AND id=$2"
        row = await conn.fetchrow(query + (" FOR UPDATE" if lock else ""), who.tenant_id, id)
        if row is None:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "资源不存在")
        role = await self._role(conn, who, row["legal_entity_id"])
        assigned = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON (s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.document_id=$3 AND s.approver_user_id=$4)",
            who.tenant_id,
            row["legal_entity_id"],
            id,
            who.user_id,
        )
        if (
            row["owner_id"] != who.user_id
            and role != "reviewer"
            and not (role == "approver" and assigned)
        ):
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "资源不存在")
        return row

    def _owner_edit(self, who: FinancePrincipal, row: Any, version: int) -> None:
        """修订和分配只允许本人编辑可修改状态。"""
        if row["owner_id"] != who.user_id:
            raise FinanceError("OWNER_REQUIRED", 403, "仅申请人可修改")
        if row["version"] != version:
            raise FinanceError("VERSION_CONFLICT", 409, "报销版本已变化")
        if row["status"] not in {"draft", "rejected", "withdrawn"}:
            raise FinanceError("CLAIM_NOT_EDITABLE", 409, "报销当前不可修改")

    async def _claim_snapshot(self, conn: Connection, row: Any) -> dict[str, Any]:
        """业务单据投影保留完整草稿、分配历史及当前流程。"""
        allocations = await conn.fetch(
            "SELECT id FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 ORDER BY id",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
        )
        result = {
            "id": row["id"],
            "kind": "expense_claim",
            "legal_entity_id": row["legal_entity_id"],
            "version": row["version"],
            "status": row["status"],
            "total": money(row["total"]),
            "owner_id": row["owner_id"],
            "record_mode": row["record_mode"],
            "content": decode(row["content"]),
            "allocation_ids": [item["id"] for item in allocations],
            "evidence": await self._claim_evidence(conn, row),
        }
        workflow = await conn.fetchrow(
            "SELECT * FROM finance_workflow WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND target_type<>'action_plan' ORDER BY target_version DESC LIMIT 1",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
        )
        if workflow is not None:
            result["workflow_id"] = workflow["id"]
            result["workflow_version"] = workflow["version"]
            result["due_at"] = workflow["due_at"].isoformat()
            steps = await conn.fetch(
                "SELECT * FROM finance_workflow_step WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 ORDER BY position",
                row["tenant_id"],
                row["legal_entity_id"],
                workflow["id"],
            )
            result["workflow"] = {
                key: workflow[key]
                for key in ("id", "version", "status", "target_version", "policy_version")
            }
            result["workflow"].update(
                {
                    "due_at": workflow["due_at"].isoformat(),
                    "provider": "internal",
                    "current_step_id": next(
                        (step["id"] for step in steps if step["status"] == "pending"), None
                    ),
                    "steps": [
                        {
                            key: step[key]
                            for key in ("id", "version", "name", "approver_user_id", "status")
                        }
                        for step in steps
                    ],
                }
            )
        return result

    async def _claim_evidence(self, conn: Connection, row: Any) -> list[dict[str, Any]]:
        """从关联票据的实际原件回执汇总证据，保留未查验含义。"""
        records = await conn.fetch(
            "SELECT DISTINCT r.id,r.sha256,r.file_ref FROM finance_allocation a JOIN finance_invoice_receipt l ON (l.tenant_id=a.tenant_id AND l.legal_entity_id=a.legal_entity_id AND l.invoice_id=a.invoice_id) JOIN finance_receipt r ON (r.tenant_id=l.tenant_id AND r.legal_entity_id=l.legal_entity_id AND r.id=l.receipt_id) WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.document_id=$3 AND a.state<>'released' UNION SELECT r.id,r.sha256,r.file_ref FROM finance_expense_evidence e JOIN finance_receipt r ON(r.tenant_id=e.tenant_id AND r.legal_entity_id=e.legal_entity_id AND r.id=e.receipt_id) WHERE e.tenant_id=$1 AND e.legal_entity_id=$2 AND e.document_id=$3 AND e.receipt_id IN(SELECT jsonb_array_elements_text(content->'evidence_ids') FROM finance_expense_claim WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3) ORDER BY id",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
        )
        return [
            {
                "id": record["id"],
                "kind": "source",
                "sha256": record["sha256"],
                "source_version": str(decode(record["file_ref"])["revision"]),
                "verification_state": "not_checked",
            }
            for record in records
        ]

    async def _validate_receipts(
        self, conn: Connection, who: FinancePrincipal, entity: str, evidence: list[str]
    ) -> None:
        """附件必须是同公司真实回执且当前员工实际可见。"""
        for id in evidence:
            row = await conn.fetchrow(
                "SELECT * FROM finance_receipt WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR SHARE",
                who.tenant_id,
                entity,
                id,
            )
            await self._authorize(conn, who, row)

    async def _link_receipts(self, conn: Connection, row: Any, evidence: list[str]) -> None:
        """逐版本保留附件关联，即使未来修订移除也不删除历史。"""
        await conn.executemany(
            "INSERT INTO finance_expense_evidence VALUES($1,$2,$3,$4,$5)",
            [
                (row["tenant_id"], row["legal_entity_id"], row["id"], row["version"], id)
                for id in evidence
            ],
        )

    async def _save_history(self, conn: Connection, row: Any) -> dict[str, Any]:
        """同事务保存每次claim版本的不可变快照。"""
        result = await self._claim_snapshot(conn, row)
        await conn.execute(
            "INSERT INTO finance_expense_history(tenant_id,legal_entity_id,id,version,data) VALUES($1,$2,$3,$4,$5::jsonb)",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
            row["version"],
            json.dumps(result),
        )
        return result

    async def _bump_claim(self, conn: Connection, row: Any, *, status: str | None = None) -> Any:
        """单据实质变化才增加版本，审批中间步骤不调用。"""
        return await conn.fetchrow(
            "UPDATE finance_expense_claim SET version=version+1,status=COALESCE($4,status) WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
            status,
        )
