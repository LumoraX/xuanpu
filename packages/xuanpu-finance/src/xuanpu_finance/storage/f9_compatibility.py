"""内部记录启动前的只读持久库兼容检查，禁止迁移掩盖既有通用历史。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.f9_schema_inventory import REQUIRED_F8_SCHEMA

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection

# 明确列名的查询同时验证旧结构；未知缺表/缺列只能拒绝，不能尝试修复。
INCOMPATIBLE_QUERIES = (
    "SELECT EXISTS(SELECT 1 FROM finance_expense_claim WHERE document_kind IS DISTINCT FROM 'expense_claim' OR record_mode IS DISTINCT FROM 'internal_record' OR simulation)",
    "SELECT EXISTS(SELECT source_simulation FROM finance_invoice)",
    "SELECT EXISTS(SELECT 1 FROM finance_operation WHERE purpose IS DISTINCT FROM 'supporting_material' OR source_simulation)",
    "SELECT EXISTS(SELECT simulation FROM finance_settlement_operation)",
    "SELECT EXISTS(SELECT simulation FROM finance_compliance_operation)",
    "SELECT EXISTS(SELECT 1 FROM finance_compliance_plan)",
    "SELECT EXISTS(SELECT 1 FROM finance_action_plan WHERE data->>'target_type' IS DISTINCT FROM 'expense_claim' OR COALESCE(data->>'action','') NOT IN ('submit_claim','approve','reject','withdraw'))",
    *('SELECT EXISTS(SELECT 1 FROM ' + name + ' WHERE enabled)' for name in (
        'finance_funding_policy', 'finance_settlement_policy', 'finance_compliance_policy',
        'finance_settlement_provider', 'finance_compliance_provider')),
    "SELECT EXISTS(SELECT 1 FROM finance_settlement_mode WHERE simulation)",
    "SELECT EXISTS(SELECT 1 FROM finance_compliance_mode WHERE simulation)",
    "SELECT EXISTS(SELECT 1 FROM finance_payment WHERE simulation)",
    "SELECT EXISTS(SELECT 1 FROM finance_posting WHERE simulation)",
    "SELECT EXISTS(SELECT 1 FROM finance_settlement_provider WHERE (data->>'simulation')::boolean)",
    "SELECT EXISTS(SELECT 1 FROM finance_compliance_provider WHERE (data->>'simulation')::boolean)",
)


def incompatible() -> FinanceError:
    """固定错误不含数据库名、schema、表名或原始驱动异常。"""
    return FinanceError('INTERNAL_DATABASE_INCOMPATIBLE', 422,
                        '既有数据库无法确认为仅内部记录，请使用独立兼容库')


async def check_internal_database(pool: asyncpg.Pool) -> None:
    """在迁移及配置写入前以只读repeatable_read检查当前实例所有租户和公司。"""
    try:
        async with pool.acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name LIKE 'finance\\_%' ESCAPE '\\'")
            if not tables:
                return
            await check_supported_schema(conn)
            for query in INCOMPATIBLE_QUERIES:
                if await conn.fetchval(query):
                    raise incompatible()
            await check_simulation_columns(conn)
    except (asyncpg.PostgresError, ValueError, TypeError, KeyError):
        raise incompatible() from None


async def check_simulation_columns(conn: Connection) -> None:
    """逐列核对全部持久模拟标记，不因某租户不在本次配置而漏查。"""
    rows = await conn.fetch(
        """SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name LIKE 'finance\\_%' ESCAPE '\\'
        AND column_name IN ('simulation','source_simulation')""")
    for row in rows:
        # 名称仅来自数据库目录，双引号转义避免目录标识符成为SQL代码。
        table = '"' + row['table_name'].replace('"', '""') + '"'
        column = '"' + row['column_name'].replace('"', '""') + '"'
        if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM ' + table + ' WHERE ' + column + ')'):
            raise incompatible()


async def check_supported_schema(conn: Connection) -> None:
    """同一只读快照逐列核对固定F8清单，缺表、缺列、额外结构和类型漂移均拒绝。"""
    rows = await conn.fetch(
        """SELECT c.relname AS table_name,a.attname AS column_name,
        pg_catalog.format_type(a.atttypid,a.atttypmod) AS data_type
        FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON(c.oid=a.attrelid)
        JOIN pg_catalog.pg_namespace n ON(n.oid=c.relnamespace)
        WHERE n.nspname=current_schema() AND c.relkind='r'
        AND c.relname LIKE 'finance\\_%' ESCAPE '\\'
        AND a.attnum>0 AND NOT a.attisdropped""")
    actual: dict[str, dict[str, str]] = {}
    for row in rows:
        actual.setdefault(row['table_name'], {})[row['column_name']] = row['data_type']
    if actual != REQUIRED_F8_SCHEMA:
        raise incompatible()
