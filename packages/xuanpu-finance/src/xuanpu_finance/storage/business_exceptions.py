"""异常证据追加与有限人工warning处置。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.business_execution import ExecutionStore
from xuanpu_finance.storage.common import identifier

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ExceptionResolution
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection

RESOLVABLE_WARNINGS = frozenset({"MANUAL_REVIEW_WARNING"})


class ExpenseStore(ExecutionStore):
    """硬约束异常的解除仍由原始业务检查负责，不能人工掩盖。"""

    async def resolve_exception(
        self, who: FinancePrincipal, id: str, body: ExceptionResolution, key: str
    ) -> dict[str, Any]:
        """真实证据只追加；服务识别warning需人工reviewer明确决定。"""
        request = {"id": id, **body.model_dump(mode="json")}
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM finance_exception WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
                who.tenant_id,
                id,
            )
            row = await self._authorize(conn, who, row)
            entity = row["legal_entity_id"]
            previous = await self._business_previous(
                conn, who, entity, "resolveException", key, request
            )
            if previous is not None:
                return previous
            if row["version"] != body.expected_version or row["status"] in {"accepted", "resolved"}:
                raise FinanceError("VERSION_CONFLICT", 409, "异常版本或状态已变化")
            status = await self._exception_disposition(conn, who, row, body)
            await self._validate_receipts(conn, who, entity, body.evidence_ids)
            for ref in body.evidence_ids:
                await conn.execute(
                    "INSERT INTO finance_exception_evidence VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                    who.tenant_id,
                    entity,
                    id,
                    ref,
                )
            await conn.execute(
                "INSERT INTO finance_exception_decision VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)",
                who.tenant_id,
                entity,
                identifier(),
                id,
                row["version"] + 1,
                who.user_id,
                json.dumps({**request, "origin": who.origin}),
            )
            row = await conn.fetchrow(
                "UPDATE finance_exception SET version=version+1,status=$4 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                who.tenant_id,
                entity,
                id,
                status,
            )
            await self._event(conn, row, "exception." + body.disposition, request)
            await self._business_operation(conn, who, entity, "exception", id)
            result = await self._exception_case(conn, row)
            return await self._business_remember(
                conn, who, entity, "resolveException", key, request, result
            )

    async def _exception_disposition(
        self, conn: Connection, who: FinancePrincipal, row: Any, body: ExceptionResolution
    ) -> str:
        """只允许处置服务明确识别的warning，硬异常不可伪造成功。"""
        if not body.evidence_ids:
            raise FinanceError("EVIDENCE_REQUIRED", 422, "处置必须提供真实证据")
        if body.disposition == "attach_evidence":
            return "investigating"
        if row["severity"] != "warning" or row["rule_id"] not in RESOLVABLE_WARNINGS:
            raise FinanceError("HARD_BLOCK", 409, "硬约束或未知规则不能通过人工处置解除")
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "warning处置必须由真实员工决定")
        if await self._role(conn, who, row["legal_entity_id"]) != "reviewer":
            raise FinanceError("REVIEWER_REQUIRED", 403, "仅公司reviewer可处置warning")
        return "accepted" if body.disposition == "accept_warning" else "resolved"
