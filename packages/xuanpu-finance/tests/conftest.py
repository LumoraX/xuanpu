"""使用独立临时数据库验证真实 PostgreSQL 行为。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from xuanpu_finance.storage import FinanceStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest_asyncio.fixture
async def store() -> AsyncIterator[FinanceStore]:
    """在显式独立测试库的随机 schema 内迁移并清理。"""
    dsn = os.environ.get("FINANCE_TEST_DSN")
    if not dsn:
        pytest.skip("须显式提供独立 FINANCE_TEST_DSN")
    schema = "finance_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    actual_database = await admin.fetchval("SELECT current_database()")
    if not actual_database.startswith("finance_test"):
        await admin.close()
        pytest.fail("安全门禁：只允许 finance_test 开头的独立测试数据库")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=8, server_settings={"search_path": schema}
    )
    assert pool is not None
    service = FinanceStore(pool)
    await service.migrate()
    try:
        yield service
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
