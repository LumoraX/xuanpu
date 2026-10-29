"""真实 PostgreSQL 验证进程独占、启动和资源释放。"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.config import FinanceConfig
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.runtime import open_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


def config_for(directory: Path, dsn: str) -> FinanceConfig:
    """凭据只写入临时目录，使用独立测试库 schema。"""
    directory.mkdir()
    (directory / "dsn").write_text(dsn)
    (directory / "token").write_text("test-token")
    (directory / "gateway-token").write_text("test-gateway-token")
    (directory / "object-key").write_bytes(b"k" * 32)
    (directory / "public.pem").write_bytes(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return FinanceConfig.model_validate(
        {
            "database_dsn_file": directory / "dsn",
            "service_token_file": directory / "token",
            "issuer": "platform",
            "audience": "finance",
            "public_key_files": {"k": directory / "public.pem"},
            "gateway": {
                "base_url": "http://127.0.0.1:1",
                "tenant_token_files": {"test": directory / "gateway-token"},
            },
            "objects": {
                "kind": "local",
                "root": directory / "originals",
                "key_file": directory / "object-key",
            },
            "companies": [
                {
                    "tenant_id": "test",
                    "legal_entity_id": "test-company",
                    "name": "测试公司",
                    "tax_id": "TEST",
                }
            ],
            "grants": [],
            "environment": "test",
            "mode": "read_only",
        }
    )


async def test_runtime_starts_without_self_deadlock_and_rejects_second_instance(
    store: FinanceStore, tmp_path: Path
) -> None:
    """启动配置事务不竞争持有者锁；第二实例拒绝，退出后能重启。"""
    parts = urlsplit(os.environ["FINANCE_TEST_DSN"])
    schema = await store.pool.fetchval("SELECT current_schema()")
    dsn = urlunsplit(
        parts._replace(query=urlencode(parse_qsl(parts.query) + [("search_path", schema)]))
    )
    config = config_for(tmp_path / "runtime", dsn)
    async with asyncio.timeout(5), open_runtime(config) as app:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as c:
                assert (await c.get("/health")).json() == {"status": "ok", "mode": "read_only"}
            with pytest.raises(FinanceError, match="instance_owned"):
                async with open_runtime(config):
                    pytest.fail("第二实例不应取得同一个数据库")
    async with asyncio.timeout(5), open_runtime(config) as app:
        assert app.state.finance_store is not None


async def test_lost_instance_lock_disables_old_runtime(store: FinanceStore, tmp_path: Path) -> None:
    """只终止本测试持锁会话，旧服务立即503且不能继续认领。"""
    parts = urlsplit(os.environ["FINANCE_TEST_DSN"])
    schema = await store.pool.fetchval("SELECT current_schema()")
    dsn = urlunsplit(
        parts._replace(query=urlencode(parse_qsl(parts.query) + [("search_path", schema)]))
    )
    config = config_for(tmp_path / "lost-lock", dsn)
    async with open_runtime(config) as app, app.router.lifespan_context(app):
        assert await store.pool.fetchval("SELECT pg_terminate_backend($1)", app.state.lock_pid)
        async with asyncio.timeout(3):
            while app.state.worker_error is None:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            assert (await c.get("/health")).status_code == 503
        assert app.state.mode == "disabled"
        assert app.state.worker_task is None or app.state.worker_task.cancelling()
        assert app.state.notification_task is None or app.state.notification_task.cancelling()
        async with open_runtime(config):
            pass
