"""财务持久化内部授权及序列化基础。"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import operation_snapshot

if TYPE_CHECKING:
    import asyncpg

    from xuanpu_finance.models import FinancePrincipal


def identifier() -> str:
    """生成不暴露数据库序号的资源标识。"""
    return uuid4().hex


def decode(value: Any) -> dict[str, Any]:
    """解码 asyncpg 默认 JSONB 文本。"""
    result: dict[str, Any] = json.loads(value) if isinstance(value, str) else dict(value)
    return result


def page(items: list[dict[str, Any]], total: int, next_cursor: str | None) -> dict[str, Any]:
    """返回独立全量计数以及最后一项的稳定游标。"""
    return {"items": items, "total": total, "next_cursor": next_cursor}


def encode_cursor(scope: str, after: list[str]) -> str:
    """编码带查询范围及版本的键集游标。"""
    raw = json.dumps({"v": 1, "scope": scope, "after": after}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, scope: str, limit: int) -> list[str] | None:
    """验证游标结构和查询作用域，禁止跨公司或筛选复用。"""
    if not 1 <= limit <= 100:
        raise FinanceError("PAGINATION_INVALID", 400, "分页大小无效")
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 2048 or not cursor.isascii():
            raise ValueError("游标长度无效")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"v", "scope", "after"}
            or type(value["v"]) is not int
            or value["v"] != 1
            or value["scope"] != scope
        ):
            raise ValueError("游标范围不符")
        after = value["after"]
        if (
            not isinstance(after, list)
            or not after
            or not all(isinstance(part, str) and 0 < len(part) <= 128 for part in after)
        ):
            raise ValueError("游标排序键无效")
        return after
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise FinanceError("CURSOR_INVALID", 400, "分页游标无效或不属于本查询") from exc


def timestamp_cursor(
    cursor: str | None, scope: str, limit: int
) -> tuple[datetime | None, str | None]:
    """还原 received_at 与标识的复合排序键。"""
    after = decode_cursor(cursor, scope, limit)
    if after is None:
        return None, None
    try:
        if len(after) != 2:
            raise ValueError("复合键长度错误")
        stamp = datetime.fromisoformat(after[0])
        if stamp.tzinfo is None:
            raise ValueError("时间缺少时区")
        return stamp, after[1]
    except ValueError as exc:
        raise FinanceError("CURSOR_INVALID", 400, "分页时间键无效") from exc


def position_cursor(cursor: str | None, scope: str, limit: int) -> int:
    """明细与字段在票据事务后不可变，按持久 position 续读。"""
    after = decode_cursor(cursor, scope, limit)
    if after is None:
        return -1
    if len(after) != 1 or not after[0].isascii() or not after[0].isdigit() or len(after[0]) > 12:
        raise FinanceError("CURSOR_INVALID", 400, "分页位置键无效")
    return int(after[0])


class FinanceBase:
    """共享连接池、公司授权和同事务事件基础。"""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """只接受外部显式注入的独立财务数据库连接池。"""
        self.pool = pool

    async def _role(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        who: FinancePrincipal,
        entity: str,
    ) -> str:
        """在业务库查公司角色，绝不默认信任平台管理员。"""
        role = await conn.fetchval(
            "SELECT role FROM finance_grant WHERE tenant_id=$1 AND user_id=$2 AND legal_entity_id=$3 FOR SHARE",
            who.tenant_id,
            who.user_id,
            entity,
        )
        if role is None:
            raise FinanceError("COMPANY_FORBIDDEN", 403, "未获公司财务授权")
        return str(role)

    async def _sync_grant_epochs(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    ) -> None:
        """保留撤权墓碑，恢复同角色不能复活旧审批指派。"""
        await conn.execute(
            "INSERT INTO finance_grant_epoch(tenant_id,legal_entity_id,user_id,role) SELECT tenant_id,legal_entity_id,user_id,role FROM finance_grant ORDER BY tenant_id,legal_entity_id,user_id ON CONFLICT(tenant_id,legal_entity_id,user_id) DO UPDATE SET role=EXCLUDED.role,generation=CASE WHEN finance_grant_epoch.role IS DISTINCT FROM EXCLUDED.role THEN finance_grant_epoch.generation+1 ELSE finance_grant_epoch.generation END"
        )
        await conn.execute(
            "UPDATE finance_grant_epoch e SET role=NULL,generation=generation+1 WHERE e.role IS NOT NULL AND NOT EXISTS(SELECT 1 FROM finance_grant g WHERE g.tenant_id=e.tenant_id AND g.legal_entity_id=e.legal_entity_id AND g.user_id=e.user_id)"
        )
        await conn.execute(
            "UPDATE finance_execution_grant g SET active=false WHERE active AND EXISTS(SELECT 1 FROM finance_workflow_step s LEFT JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id) WHERE s.tenant_id=g.tenant_id AND s.legal_entity_id=g.legal_entity_id AND s.workflow_id=g.workflow_id AND (e.role IS NULL OR e.role NOT IN ('approver','reviewer') OR e.generation<>s.authority_generation))"
        )

    async def _authorize(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        who: FinancePrincipal,
        row: asyncpg.Record | None,
    ) -> asyncpg.Record:
        """要求租户一致，且为本人记录或公司 reviewer。"""
        if row is None or row["tenant_id"] != who.tenant_id:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "资源不存在")
        role = await self._role(conn, who, row["legal_entity_id"])
        if row["owner_id"] != who.user_id and role != "reviewer":
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "资源不存在")
        return row

    async def _authorize_invoice(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        who: FinancePrincipal,
        row: Any,
    ) -> Any:
        """审批人仅看实际分派单据关联的票据，不能取得公司全票读权。"""
        if row is None or row["tenant_id"] != who.tenant_id:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "资源不存在")
        role = await self._role(conn, who, row["legal_entity_id"])
        assigned = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_allocation a JOIN finance_workflow w ON(w.tenant_id=a.tenant_id AND w.legal_entity_id=a.legal_entity_id AND w.document_id=a.document_id) JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.invoice_id=$3 AND s.approver_user_id=$4)",
            who.tenant_id,
            row["legal_entity_id"],
            row["id"],
            who.user_id,
        )
        if (
            row["owner_id"] != who.user_id
            and role != "reviewer"
            and not (role == "approver" and assigned)
        ):
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "资源不存在")
        return row

    async def _invoice_snapshot(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        tenant: str,
        entity: str,
        value: Any,
    ) -> dict[str, Any]:
        """可用金额实时扣除reserved和consumed，永不覆盖票面或查验状态。"""
        data = decode(value)
        used = await conn.fetchval(
            "SELECT COALESCE(sum(amount),0) FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND state IN ('reserved','consumed')",
            tenant,
            entity,
            data["id"],
        )
        data["available_amount"] = format(Decimal(data["gross"]) - used, ".2f")
        return data

    async def _exception_case(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        row: Any,
    ) -> dict[str, Any]:
        """异常写入和列表共用同一当前版本契约。"""
        return {
            "id": row["id"],
            "version": row["version"],
            "legal_entity_id": row["legal_entity_id"],
            "rule_id": row["rule_id"],
            "severity": row["severity"],
            "status": row["status"],
            "evidence": await self._exception_evidence(conn, row),
        }

    async def _exception_evidence(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        row: Any,
    ) -> list[dict[str, Any]]:
        """异常原始回执和后续追加材料分别保留同一未查验来源语义。"""
        records = await conn.fetch(
            "SELECT r.* FROM finance_receipt r WHERE r.tenant_id=$1 AND r.legal_entity_id=$2 AND (r.id=$3 OR EXISTS(SELECT 1 FROM finance_exception_evidence e WHERE e.tenant_id=r.tenant_id AND e.legal_entity_id=r.legal_entity_id AND e.receipt_id=r.id AND e.exception_id=$4)) ORDER BY r.id",
            row["tenant_id"],
            row["legal_entity_id"],
            row["receipt_id"],
            row["id"],
        )
        return [
            {
                "id": record["id"],
                "kind": "source",
                "sha256": record["sha256"],
                "verification_state": "not_checked",
            }
            for record in records
        ]

    async def _snapshot(
        self, conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, row: Any
    ) -> dict[str, Any]:
        """读取同一操作的持久逐项进度。"""
        items = await conn.fetch(
            "SELECT * FROM finance_item WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3 ORDER BY position",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
        )
        return operation_snapshot(row, list(items))

    async def _event(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        row: Any,
        kind: str,
        data: object,
    ) -> None:
        """在当前事务内写业务事件，失败由事务回滚承担。"""
        await conn.execute(
            "INSERT INTO finance_event(tenant_id,legal_entity_id,aggregate_id,kind,data) VALUES($1,$2,$3,$4,$5::jsonb)",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
            kind,
            json.dumps(data),
        )
