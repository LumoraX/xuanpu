"""真实F1旧数据迁移到F2，环境与业务资格不能自动升级。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from xuanpu_finance.storage.f1_schema import F1_SCHEMA
from xuanpu_finance.storage.f2_schema import F2_INBOX_SCHEMA, F2_SCHEMA
from xuanpu_finance.storage.schema import SCHEMA


async def test_real_f1_rows_survive_repeated_f2_migration_without_execution_upgrade(
    store: Any,
) -> None:
    """保留已批准内部记录及历史版本，既有单据默认正式标记但没有执行资格。"""
    schema = "f2_upgrade_" + uuid4().hex
    async with store.pool.acquire() as conn, conn.transaction():
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET LOCAL search_path TO "{schema}"')
        await conn.execute(SCHEMA)
        await conn.execute(F1_SCHEMA)
        await conn.execute("INSERT INTO finance_company VALUES('old','old','旧公司','OLD')")
        await conn.execute(
            "INSERT INTO finance_expense_claim(tenant_id,legal_entity_id,id,owner_id,version,status,total,content,record_mode) VALUES('old','old','legacy','owner',4,'approved',25.00,'{}','internal_record')"
        )
        await conn.execute(
            "INSERT INTO finance_expense_history(tenant_id,legal_entity_id,id,version,data) VALUES('old','old','legacy',4,'{\"reason\":\"旧记录\"}')"
        )
        for _ in range(2):
            await conn.execute(F2_SCHEMA)
            await conn.execute(F2_INBOX_SCHEMA)
        row = await conn.fetchrow("SELECT * FROM finance_expense_claim WHERE id='legacy'")
        assert row["version"] == 4 and row["status"] == "approved"
        assert row["document_kind"] == "expense_claim" and row["record_mode"] == "internal_record"
        assert row["simulation"] is False
        assert await conn.fetchval("SELECT data->>'reason' FROM finance_expense_history") == "旧记录"
        assert await conn.fetchval("SELECT count(*) FROM finance_settlement_operation") == 0
        assert await conn.fetchval("SELECT count(*) FROM finance_execution_grant") == 0
        assert await conn.fetchval("SELECT count(*) FROM finance_settlement_mode") == 0
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
