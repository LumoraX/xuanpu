"""持久审批回执：会话行锁串行认领，审计与结算同事务。"""
from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import NotFoundError
from xuanpu_server.runtime.operation_receipts import (
    OperationReceipt,
    decision_fingerprint,
    replay_receipt,
)

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

    from xuanpu_server.controlplane.models import AuditRecord


class PostgresReceiptStore:
    """数据库内原子认领，独立于审计消费延迟。"""

    def __init__(self, pool: asyncpg.Pool, namespace: SqlNamespace) -> None:
        """复用控制面连接池与已校验的命名空间。"""
        self._pool = pool
        self._table = namespace.table("operation_receipt")
        self._sessions = namespace.table("session")
        self._audit = namespace.table("audit")

    async def claim(self, tenant: str, session: str, decisions: dict[str, Any], *, at: float) -> OperationReceipt:
        """整组认领先锁会话，不因部分重叠留下半组记录。"""
        fingerprint = decision_fingerprint(decisions)
        ids = sorted(decisions)
        async with self._pool.acquire() as conn, conn.transaction():
            found = await conn.fetchval(f"SELECT session_id FROM {self._sessions} "
                "WHERE tenant_id=$1 AND session_id=$2 FOR UPDATE", tenant, session)
            if found is None:
                raise NotFoundError("会话不存在")
            await conn.execute(f"DELETE FROM {self._table} "
                "WHERE tenant_id=$1 AND session_id=$2 AND request_id=ANY($3::text[]) AND state='rejected'",
                tenant, session, ids)
            rows = await conn.fetch(f"SELECT operation_id,fingerprint,state,submission_id FROM {self._table} "
                "WHERE tenant_id=$1 AND session_id=$2 AND request_id=ANY($3::text[])", tenant, session, ids)
            if rows:
                prior = [OperationReceipt(row["operation_id"], row["fingerprint"], row["state"], row["submission_id"]) for row in rows]
                return replay_receipt(prior, fingerprint, len(ids))
            receipt = OperationReceipt("op_" + secrets.token_hex(16), fingerprint)
            await conn.executemany(f"INSERT INTO {self._table} "
                "(tenant_id,session_id,request_id,operation_id,fingerprint,state,created_at,updated_at) "
                "VALUES ($1,$2,$3,$4,$5,'claimed',$6,$6)",
                [(tenant, session, key, receipt.operation_id, fingerprint, at) for key in ids])
            return receipt

    async def submitted(self, tenant: str, operation: str, submission: str, *, at: float) -> None:
        """提交标识可以迟到，但不能回退已经核实的核销。"""
        async with self._pool.acquire() as conn:
            await conn.execute(f"UPDATE {self._table} SET submission_id=$3,updated_at=$4,"
                "state=CASE WHEN state='claimed' THEN 'submitted' ELSE state END "
                "WHERE tenant_id=$1 AND operation_id=$2", tenant, operation, submission, at)

    async def settle(self, tenant: str, operation: str, state: str, audit: AuditRecord | None, *, at: float) -> None:
        """回执与追加审计要么一起提交，要么一起回滚。"""
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(f"SELECT state FROM {self._table} "
                "WHERE tenant_id=$1 AND operation_id=$2 FOR UPDATE", tenant, operation)
            if not any(row["state"] != state and not (row["state"] == "applied" and state != "applied") for row in rows):
                return
            if audit is not None:
                if audit.tenant_id != tenant:
                    raise ValueError("回执审计租户不匹配")
                await conn.execute(f"INSERT INTO {self._audit} "
                    "(tenant_id,source,kind,at,actor,session_id,data,user_id) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)",
                    tenant, audit.source, audit.kind, audit.at, audit.actor, audit.session_id,
                    json.dumps(audit.data, ensure_ascii=False, allow_nan=False), audit.user_id)
            await conn.execute(f"UPDATE {self._table} SET state=$3,updated_at=$4 "
                "WHERE tenant_id=$1 AND operation_id=$2 AND (state <> 'applied' OR $3='applied')",
                tenant, operation, state, at)

    async def release(self, tenant: str, operation: str) -> None:
        """只有确认未越过提交边界的认领可以释放。"""
        async with self._pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {self._table} WHERE tenant_id=$1 AND operation_id=$2 "
                "AND (state='rejected' OR (state='claimed' AND submission_id IS NULL))", tenant, operation)
