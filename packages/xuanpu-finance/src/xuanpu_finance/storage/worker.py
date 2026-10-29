"""持久批次租约、逐原件检查点与票据提交的事务边界。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.imports import validate_source_environment
from xuanpu_finance.storage.originals import OriginalStore

if TYPE_CHECKING:
    import asyncpg

    from xuanpu_finance.invoices.parser import ParsedInvoice


class WorkerStore(OriginalStore):
    """通过持久租约令牌阻止过期 worker 重复提交。"""

    async def claim_import(self) -> dict[str, Any] | None:
        """认领一个未完成或租约过期的批次，跳过其他实例持有的行锁。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM finance_operation WHERE status IN ('accepted','running') AND (lease_until IS NULL OR lease_until < now()) ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            if row is None:
                return None
            row = await conn.fetchrow(
                "UPDATE finance_operation SET status='running',version=version+1,lease_token=$2,lease_until=now()+interval '60 seconds' WHERE id=$1 RETURNING *",
                row["id"],
                identifier(),
            )
            assert row is not None
            await self._event(conn, row, "import.claimed", {})
            return dict(row)

    async def renew_import(self, claim: dict[str, Any]) -> None:
        """只续租仍由本 worker 持有且未过期的操作。"""
        result = await self.pool.execute(
            "UPDATE finance_operation SET lease_until=now()+interval '60 seconds' WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND lease_token=$4 AND lease_until>now() AND status='running'",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
            claim["lease_token"],
        )
        if result != "UPDATE 1":
            raise FinanceError("LEASE_LOST", 409, "批次租约已丢失")

    async def release_import(self, claim: dict[str, Any]) -> None:
        """停机、失败或完成后释放本 worker 的租约。"""
        await self.pool.execute(
            "UPDATE finance_operation SET lease_token=NULL,lease_until=NULL WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND lease_token=$4",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
            claim["lease_token"],
        )

    async def pending_items(self, claim: dict[str, Any]) -> list[dict[str, Any]]:
        """恢复时只读取尚未提交的项，保留已提交票。"""
        rows = await self.pool.fetch(
            "SELECT * FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND state IN ('pending','received') ORDER BY position",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
        )
        return [dict(row) for row in rows]

    async def ensure_worker_access(self, claim: dict[str, Any]) -> None:
        """后台每项处理前重新检查财务公司授权。"""
        async with self.pool.acquire() as conn:
            await self._role(
                conn,
                FinancePrincipal(claim["tenant_id"], claim["owner_id"]),
                claim["legal_entity_id"],
            )

    async def finish_item_error(
        self, claim: dict[str, Any], position: int, state: str, code: str
    ) -> None:
        """将责任明确的失败/待补件持久化，异常与检查点同事务。"""
        if state not in {"failed", "quarantined", "awaiting_original"}:
            raise ValueError("无效的失败项状态")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._locked_claim(conn, claim)
            item = await conn.fetchrow(
                "SELECT * FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4 FOR UPDATE",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                position,
            )
            assert item is not None
            if item["state"] not in {"pending", "received"}:
                return
            exception_id = identifier()
            rule = "ORIGINAL_REQUIRED" if state == "awaiting_original" else code
            await conn.execute(
                "INSERT INTO finance_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,item_position,rule_id,receipt_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
                row["tenant_id"],
                row["legal_entity_id"],
                exception_id,
                row["owner_id"],
                row["id"],
                position,
                rule,
                item["receipt_id"],
            )
            await conn.execute(
                "UPDATE finance_item SET state=$5,error_code=$6,exception_id=$7 WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                position,
                state,
                code,
                exception_id,
            )
            await conn.execute(
                "UPDATE finance_operation SET version=version+1 WHERE id=$1", row["id"]
            )
            await self._event(
                conn,
                row,
                "import.item_exception",
                {"position": position, "exception_id": exception_id, "code": code},
            )

    async def _supplement_source(
        self, conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, row: Any
    ) -> Any:
        """提交票前锁定源材料缺失异常，阻止并发补件误关异常。"""
        if row["source_exception_id"] is None:
            return None
        source = await conn.fetchrow(
            "SELECT * FROM finance_exception WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE",
            row["tenant_id"],
            row["legal_entity_id"],
            row["source_exception_id"],
        )
        if source is None or source["rule_id"] != "ORIGINAL_REQUIRED":
            raise FinanceError("SUPPLEMENT_CONFLICT", 409, "源材料缺失异常不存在")
        await validate_source_environment(conn, row["tenant_id"], row["legal_entity_id"], source, row["source_simulation"])
        if source["resolved_operation_id"] == row["id"]:
            return source
        if source["status"] == "resolved" or source["version"] != row["expected_exception_version"]:
            raise FinanceError("SUPPLEMENT_CONFLICT", 409, "源异常版本已变化")
        await self._authorize(conn, FinancePrincipal(row["tenant_id"], row["owner_id"]), source)
        return source

    async def finish_item_parsed(
        self, claim: dict[str, Any], position: int, parsed: ParsedInvoice
    ) -> None:
        """主票唯一约束、明细字段、原件链接、补件闭环和事件同事务提交。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._locked_claim(conn, claim)
            await self._role(
                conn, FinancePrincipal(row["tenant_id"], row["owner_id"]), row["legal_entity_id"]
            )
            source = await self._supplement_source(conn, row)
            item = await conn.fetchrow(
                "SELECT * FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4 FOR UPDATE",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                position,
            )
            assert item is not None
            if item["state"] != "received":
                raise FinanceError("ITEM_STATE_INVALID", 409, "原件尚未收到或已完成")
            invoice = {
                **parsed.invoice,
                "id": identifier(),
                "legal_entity_id": row["legal_entity_id"],
            }
            inserted = await conn.fetchrow(
                "INSERT INTO finance_invoice(tenant_id,legal_entity_id,id,owner_id,seller_tax_id,number,issue_date,data,canonical_digest,source_simulation) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10) ON CONFLICT(tenant_id,legal_entity_id,seller_tax_id,number,issue_date) DO NOTHING RETURNING id",
                row["tenant_id"],
                row["legal_entity_id"],
                invoice["id"],
                row["owner_id"],
                invoice["seller_tax_id"],
                invoice["number"],
                invoice["issue_date"],
                json.dumps(invoice),
                request_digest({"invoice": parsed.invoice, "lines": parsed.lines}),
                row["source_simulation"],
            )
            state = "parsed"
            if inserted is None:
                invoice["id"] = await self._duplicate(conn, row, parsed)
                state = "duplicate_linked"
            else:
                await self._children(conn, row, invoice["id"], item["receipt_id"], parsed)
            await conn.execute(
                "INSERT INTO finance_invoice_receipt VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                row["tenant_id"],
                row["legal_entity_id"],
                invoice["id"],
                item["receipt_id"],
            )
            await conn.execute(
                "UPDATE finance_item SET state=$5,invoice_id=$6 WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                position,
                state,
                invoice["id"],
            )
            if source is not None:
                await self._resolve_source(conn, row, source, invoice["id"])
            await conn.execute(
                "UPDATE finance_operation SET version=version+1 WHERE id=$1", row["id"]
            )
            await self._event(
                conn, row, "invoice." + state, {"invoice_id": invoice["id"], "position": position}
            )

    async def _resolve_source(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        row: Any,
        source: Any,
        invoice_id: str,
    ) -> None:
        """将旧材料与新原件共同链接到票，并在同事务内关闭源异常。"""
        if source["receipt_id"] is not None:
            await conn.execute(
                "INSERT INTO finance_invoice_receipt VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                row["tenant_id"],
                row["legal_entity_id"],
                invoice_id,
                source["receipt_id"],
            )
        if source["status"] == "resolved":
            return
        await conn.execute(
            "UPDATE finance_exception SET status='resolved',version=version+1,resolved_invoice_id=$4,resolved_operation_id=$5 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            row["tenant_id"],
            row["legal_entity_id"],
            source["id"],
            invoice_id,
            row["id"],
        )
        await self._event(
            conn,
            row,
            "exception.original_attached",
            {"source_exception_id": source["id"], "invoice_id": invoice_id},
        )

    async def _duplicate(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        row: Any,
        parsed: ParsedInvoice,
    ) -> str:
        """业务主键重复必须一致且可见，不能以同票号覆盖不同内容。"""
        invoice = parsed.invoice
        previous = await conn.fetchrow(
            "SELECT * FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND seller_tax_id=$3 AND number=$4 AND issue_date=$5",
            row["tenant_id"],
            row["legal_entity_id"],
            invoice["seller_tax_id"],
            invoice["number"],
            invoice["issue_date"],
        )
        assert previous is not None
        if previous["source_simulation"] != row["source_simulation"]:
            raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "重复原件与历史票据的实际来源环境不一致")
        if previous["canonical_digest"] != request_digest(
            {"invoice": parsed.invoice, "lines": parsed.lines}
        ):
            raise FinanceError("DUPLICATE_CONTENT_CONFLICT", 409, "票号相同但票面内容冲突")
        if previous["owner_id"] != row["owner_id"]:
            role = await self._role(
                conn, FinancePrincipal(row["tenant_id"], row["owner_id"]), row["legal_entity_id"]
            )
            if role != "reviewer":
                raise FinanceError(
                    "DUPLICATE_RESTRICTED", 409, "已有记录属于其他收集人，需公司复核人核对"
                )
        return str(previous["id"])

    async def _children(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        row: Any,
        invoice_id: str,
        receipt_id: str,
        parsed: ParsedInvoice,
    ) -> None:
        """同事务写入所有明细及逐字段原值来源。"""
        args = (row["tenant_id"], row["legal_entity_id"], invoice_id)
        await conn.executemany(
            "INSERT INTO finance_invoice_line VALUES($1,$2,$3,$4,$5::jsonb)",
            [
                (*args, index, json.dumps({**line, "id": identifier()}))
                for index, line in enumerate(parsed.lines)
            ],
        )
        await conn.executemany(
            "INSERT INTO finance_invoice_field VALUES($1,$2,$3,$4,$5::jsonb)",
            [
                (*args, index, json.dumps({**field, "source_receipt_id": receipt_id}))
                for index, field in enumerate(parsed.fields)
            ],
        )

    async def finish_supporting_item(self, claim: dict[str, Any], position: int) -> None:
        """租约和固定摘要均有效才完成材料收件，不形成发票或材料核实事实。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._locked_claim(conn, claim)
            await self._role(conn, FinancePrincipal(row["tenant_id"], row["owner_id"]), row["legal_entity_id"])
            if row["purpose"] != "supporting_material":
                raise FinanceError("IMPORT_PURPOSE_INVALID", 409, "当前请求不是材料收件")
            item = await conn.fetchrow(
                "SELECT * FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4 FOR UPDATE", row["tenant_id"], row["legal_entity_id"], row["id"], position
            )
            assert item is not None
            if item["state"] == "material_received":
                return
            if item["state"] != "received" or item["receipt_id"] is None or item["actual_sha256"] != decode(item["file_ref"])["sha256"]:
                raise FinanceError("MATERIAL_NOT_RECEIVED", 409, "固定版本材料尚未完整接收")
            await conn.execute("UPDATE finance_item SET state='material_received' WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4", row["tenant_id"], row["legal_entity_id"], row["id"], position)
            await conn.execute("UPDATE finance_operation SET version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["id"])
            await self._event(conn, row, "material.received", {"position": position, "receipt_id": item["receipt_id"]})

    async def complete_import(self, claim: dict[str, Any]) -> None:
        """处理完全部项后区分生命周期结束与业务部分失败。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._locked_claim(conn, claim)
            snapshot = await self._snapshot(conn, row)
            summary = snapshot["summary"]
            if summary["pending"]:
                raise FinanceError("IMPORT_PENDING", 409, "尚有未处理原件")
            good = summary["parsed"] + summary["duplicate_linked"]
            if row["purpose"] == "supporting_material":
                good += summary["materials"]
            result = "succeeded" if good == summary["total"] else "partial" if good else "failed"
            await conn.execute(
                "UPDATE finance_operation SET status=CASE WHEN $2='failed' THEN 'failed' ELSE 'succeeded' END,business_result=$2,version=version+1,lease_token=NULL,lease_until=NULL WHERE id=$1",
                row["id"],
                result,
            )
            await self._event(conn, row, "import.completed", {"business_result": result})
