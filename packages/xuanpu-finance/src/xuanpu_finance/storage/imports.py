"""导入受理、用户取消与幂等回执。"""

from __future__ import annotations

import json
from typing import Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FileRef, FinancePrincipal, InvoiceImport
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import FinanceBase, decode, identifier


class ImportStore(FinanceBase):
    """持久操作的入口，不在受理事务内处理原件。"""

    async def accept_import(
        self,
        who: FinancePrincipal,
        legal_entity_id: str,
        files: list[FileRef],
        key: str,
        *,
        source_exception_id: str | None = None,
        expected_exception_version: int | None = None,
    ) -> dict[str, Any]:
        """同键同内容返回已有操作，异内容拒绝，补件绑定持久异常版本。"""
        request = InvoiceImport(
            legal_entity_id=legal_entity_id,
            files=files,
            source_exception_id=source_exception_id,
            expected_exception_version=expected_exception_version,
        )
        if not key or len(key) > 128:
            raise FinanceError("IDEMPOTENCY_KEY_INVALID", 422, "幂等键长度无效")
        digest = request_digest(request.model_dump())
        async with self.pool.acquire() as conn, conn.transaction():
            await self._role(conn, who, legal_entity_id)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                request_digest([who.tenant_id, who.user_id, key]),
            )
            previous = await conn.fetchrow(
                "SELECT * FROM finance_operation WHERE tenant_id=$1 AND owner_id=$2 AND idempotency_key=$3",
                who.tenant_id,
                who.user_id,
                key,
            )
            if previous is not None:
                if previous["request_hash"] != digest:
                    raise FinanceError("IDEMPOTENCY_CONFLICT", 409, "同幂等键请求内容不同")
                return await self._snapshot(conn, previous)
            if source_exception_id is not None:
                source = await conn.fetchrow(
                    "SELECT * FROM finance_exception WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE",
                    who.tenant_id,
                    legal_entity_id,
                    source_exception_id,
                )
                source = await self._authorize(conn, who, source)
                if (
                    source["version"] != expected_exception_version
                    or source["status"] == "resolved"
                ):
                    raise FinanceError("VERSION_CONFLICT", 409, "原异常已变化")
                if source["rule_id"] != "ORIGINAL_REQUIRED":
                    raise FinanceError("SUPPLEMENT_NOT_ALLOWED", 409, "仅材料缺失异常接受原件补充")
            row = await conn.fetchrow(
                "INSERT INTO finance_operation(tenant_id,legal_entity_id,id,owner_id,idempotency_key,request_hash,trace_id,source_exception_id,expected_exception_version) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *",
                who.tenant_id,
                legal_entity_id,
                identifier(),
                who.user_id,
                key,
                digest,
                identifier(),
                source_exception_id,
                expected_exception_version,
            )
            assert row is not None
            for position, file in enumerate(files):
                await conn.execute(
                    "INSERT INTO finance_item(tenant_id,legal_entity_id,operation_id,position,file_ref) VALUES($1,$2,$3,$4,$5::jsonb)",
                    who.tenant_id,
                    legal_entity_id,
                    row["id"],
                    position,
                    file.model_dump_json(),
                )
            await self._event(
                conn,
                row,
                "import.accepted",
                {"files": len(files), "source_exception_id": source_exception_id},
            )
            return await self._snapshot(conn, row)

    async def get_operation(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """返回本人或授权 reviewer 可见的完整逐项进度。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(
                "SELECT * FROM finance_operation WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
            )
            row = await self._authorize(conn, who, row)
            return await self._snapshot(conn, row)

    async def cancel_operation(
        self, who: FinancePrincipal, id: str, expected_version: int, key: str
    ) -> dict[str, Any]:
        """事务取消未完成项，已提交票和原件回执保持可审计。"""
        digest = request_digest({"kind": "cancel", "id": id, "version": expected_version})
        if not key or len(key) > 128:
            raise FinanceError("IDEMPOTENCY_KEY_INVALID", 422, "幂等键长度无效")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                request_digest([who.tenant_id, who.user_id, key]),
            )
            row = await conn.fetchrow(
                "SELECT * FROM finance_operation WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
                who.tenant_id,
                id,
            )
            row = await self._authorize(conn, who, row)
            previous = await conn.fetchrow(
                "SELECT * FROM finance_idempotency WHERE tenant_id=$1 AND owner_id=$2 AND key=$3",
                who.tenant_id,
                who.user_id,
                key,
            )
            if previous:
                if previous["request_hash"] != digest:
                    raise FinanceError("IDEMPOTENCY_CONFLICT", 409, "同幂等键请求内容不同")
                return decode(previous["response"])
            if row["version"] != expected_version:
                raise FinanceError("VERSION_CONFLICT", 409, "操作版本已变化")
            if row["status"] not in {"accepted", "running"}:
                raise FinanceError("OPERATION_TERMINAL", 409, "操作已结束")
            await conn.execute(
                "UPDATE finance_item SET state='cancelled' WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND state IN ('pending','received')",
                who.tenant_id,
                row["legal_entity_id"],
                id,
            )
            row = await conn.fetchrow(
                "UPDATE finance_operation SET version=version+1,status='cancelled',business_result='cancelled',lease_token=NULL,lease_until=NULL WHERE tenant_id=$1 AND id=$2 RETURNING *",
                who.tenant_id,
                id,
            )
            assert row is not None
            await self._event(conn, row, "import.cancelled", {"expected_version": expected_version})
            result = await self._snapshot(conn, row)
            await conn.execute(
                "INSERT INTO finance_idempotency VALUES($1,$2,$3,$4,$5,$6::jsonb)",
                who.tenant_id,
                row["legal_entity_id"],
                who.user_id,
                key,
                digest,
                json.dumps(result),
            )
            return result
