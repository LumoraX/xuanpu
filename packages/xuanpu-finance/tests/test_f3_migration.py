"""真实PG幂等迁移、独立目标和不可变原字节约束。"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from .test_f3_domain import configured, fresh_snapshot, invoice, result_for


async def test_repeated_migration_preserves_independent_f3_receipt_and_target_tables(store: Any) -> None:
    """迁移不重置业务历史，F3没有伪造费用单据父外键。"""
    owner,_,_=await configured(store)
    await invoice(store)
    await store.verify_invoice(owner,"blue",1,"v")
    job=await store.claim_compliance()
    assert job is not None
    await result_for(store,job,fresh_snapshot(verification_id="v"))
    original=await store.pool.fetchval("SELECT raw FROM finance_compliance_receipt")
    await store.migrate()
    await store.migrate()
    assert await store.pool.fetchval("SELECT raw FROM finance_compliance_receipt") == original
    foreign=await store.pool.fetchval("SELECT count(*) FROM pg_constraint c JOIN pg_class t ON(t.oid=c.conrelid) WHERE c.contype='f' AND t.relnamespace=current_schema()::regnamespace AND t.relname LIKE 'finance_compliance_%' AND c.confrelid='finance_expense_claim'::regclass")
    assert foreign == 0
    assert await store.pool.fetchval("SELECT count(*) FROM finance_expense_claim") == 0
    with pytest.raises(asyncpg.RaiseError,match="不可变"):
        await store.pool.execute("UPDATE finance_compliance_receipt SET raw='rewritten'::bytea")
    with pytest.raises(asyncpg.RaiseError,match="不可变"):
        await store.pool.execute("DELETE FROM finance_compliance_plan")
