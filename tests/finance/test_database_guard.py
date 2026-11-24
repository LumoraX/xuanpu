"""数据库测试门禁核对真实库名，不能被DSN密码里的test字样绕过。"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import store as store_fixture


async def test_fixture_checks_database_name_before_any_write(monkeypatch: Any) -> None:
    """即使密码包含finance_test，实际platform库也不能CREATE SCHEMA。"""
    closed = []

    class Connection:
        """仅允许只读确认当前数据库，写入即判定测试失败。"""

        async def fetchval(self, query: str) -> str:
            """返回实际生产命名，区别于DSN中的诱导字符串。"""
            assert query == "SELECT current_database()"
            return "platform"

        async def execute(self, query: str) -> None:
            """禁止验证前执行任何写入。"""
            raise AssertionError("不应写入非测试库")

        async def close(self) -> None:
            """拒绝目标后也释放连接。"""
            closed.append(True)

    async def connect(dsn: str) -> Connection:
        """替代网络连接，只测测试框架自身安全门禁。"""
        return Connection()

    monkeypatch.setenv("FINANCE_TEST_DSN", "postgresql://u:finance_test@localhost/platform")
    monkeypatch.setattr("asyncpg.connect", connect)
    fixture = store_fixture.__wrapped__()
    with pytest.raises(pytest.fail.Exception, match="测试数据库"):
        await anext(fixture)
    assert closed == [True]
