"""跨数据库与对象存储的原件责任索引和可恢复回执事务。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.storage.common import FinanceBase, identifier

if TYPE_CHECKING:
    import asyncpg


class OriginalStore(FinanceBase):
    """对象写前持久责任索引，写后回执失败仍可恢复已保全副本。"""

    async def _locked_claim(
        self, conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, claim: dict[str, Any]
    ) -> Any:
        """在每个检查点重新验证租约令牌，取消优先。"""
        row = await conn.fetchrow(
            "SELECT * FROM finance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND lease_token=$4 AND lease_until>now() AND status='running' FOR UPDATE",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
            claim["lease_token"],
        )
        if row is None:
            raise FinanceError("LEASE_LOST", 409, "批次已取消或租约失效")
        return row

    async def prepare_original(
        self,
        claim: dict[str, Any],
        position: int,
        actual_sha256: str,
        size_bytes: int,
        content_type: str,
        filename: str,
    ) -> dict[str, Any]:
        """在对象写入前持久责任键、内容摘要及元数据，并预占真实批大小。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._locked_claim(conn, claim)
            await self._role(
                conn, FinancePrincipal(row["tenant_id"], row["owner_id"]), row["legal_entity_id"]
            )
            args = (row["tenant_id"], row["legal_entity_id"], row["id"], position)
            item = await conn.fetchrow(
                "SELECT * FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4 FOR UPDATE",
                *args,
            )
            assert item is not None
            if item["state"] != "pending":
                return dict(item)
            if item["object_key"] is not None:
                if (item["actual_sha256"], item["size_bytes"]) != (actual_sha256, size_bytes):
                    raise FinanceError(
                        "ORIGINAL_INDEX_CONFLICT", 409, "平台原件与已持久责任摘要不一致"
                    )
                return dict(item)
            reserved = await conn.fetchval(
                "SELECT COALESCE(sum(size_bytes),0) FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND object_key IS NOT NULL",
                *args[:3],
            )
            if size_bytes > 20 * 1024 * 1024 or reserved + size_bytes > 100 * 1024 * 1024:
                raise FinanceError("IMPORT_SIZE_EXCEEDED", 413, "原件或批次总大小超限")
            key = f"{row['tenant_id']}/{row['legal_entity_id']}/{row['id']}/{position}/source"
            item = await conn.fetchrow(
                "UPDATE finance_item SET object_key=$5,actual_sha256=$6,size_bytes=$7,content_type=$8,filename=$9 WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4 RETURNING *",
                *args,
                key,
                actual_sha256,
                size_bytes,
                content_type[:256],
                filename[:1000],
            )
            await conn.execute(
                "UPDATE finance_operation SET version=version+1 WHERE id=$1", row["id"]
            )
            await self._event(
                conn,
                row,
                "original.prepared",
                {
                    "position": position,
                    "object_key": key,
                    "sha256": actual_sha256,
                    "size_bytes": size_bytes,
                },
            )
            assert item is not None
            return dict(item)

    async def receive_original(
        self,
        claim: dict[str, Any],
        position: int,
        object_key: str,
        actual_sha256: str,
        size_bytes: int,
        content_type: str,
        filename: str,
    ) -> dict[str, Any]:
        """加密原件写成功后，原子保留回执、批大小和 received 检查点。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._locked_claim(conn, claim)
            await self._role(
                conn, FinancePrincipal(row["tenant_id"], row["owner_id"]), row["legal_entity_id"]
            )
            if (
                size_bytes > 20 * 1024 * 1024
                or row["bytes_received"] + size_bytes > 100 * 1024 * 1024
            ):
                raise FinanceError("IMPORT_SIZE_EXCEEDED", 413, "原件或批次总大小超限")
            item = await conn.fetchrow(
                "SELECT * FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4 FOR UPDATE",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
                position,
            )
            assert item is not None
            if item["state"] != "pending":
                return dict(item)
            expected = (
                item["object_key"],
                item["actual_sha256"],
                item["size_bytes"],
                item["content_type"],
                item["filename"],
            )
            actual = (object_key, actual_sha256, size_bytes, content_type[:256], filename[:1000])
            if expected != actual:
                raise FinanceError("ORIGINAL_INDEX_CONFLICT", 409, "原件回执与已持久责任索引不一致")
            receipt_id = identifier()
            args = (row["tenant_id"], row["legal_entity_id"], row["id"], position)
            await conn.execute(
                "INSERT INTO finance_receipt(tenant_id,legal_entity_id,id,owner_id,operation_id,position,object_key,sha256,file_ref) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)",
                row["tenant_id"],
                row["legal_entity_id"],
                receipt_id,
                row["owner_id"],
                row["id"],
                position,
                object_key,
                actual_sha256,
                item["file_ref"],
            )
            item = await conn.fetchrow(
                "UPDATE finance_item SET state='received',receipt_id=$5,object_key=$6,actual_sha256=$7,size_bytes=$8,content_type=$9,filename=$10 WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 AND position=$4 RETURNING *",
                *args,
                receipt_id,
                object_key,
                actual_sha256,
                size_bytes,
                content_type[:256],
                filename[:1000],
            )
            await conn.execute(
                "UPDATE finance_operation SET version=version+1,bytes_received=bytes_received+$2 WHERE id=$1",
                row["id"],
                size_bytes,
            )
            await self._event(
                conn,
                row,
                "original.received",
                {"position": position, "receipt_id": receipt_id, "sha256": actual_sha256},
            )
            assert item is not None
            return dict(item)
