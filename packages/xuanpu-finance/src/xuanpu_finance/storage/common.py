"""财务持久化内部授权及序列化基础。"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
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
            "SELECT role FROM finance_grant WHERE tenant_id=$1 AND user_id=$2 AND legal_entity_id=$3",
            who.tenant_id,
            who.user_id,
            entity,
        )
        if role is None:
            raise FinanceError("COMPANY_FORBIDDEN", 403, "未获公司财务授权")
        return str(role)

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
