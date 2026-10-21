"""模拟执行必须隔离数据库和原件空间，不能靠配置名自称隔离。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from xuanpu_finance import runtime
from xuanpu_finance.config import FinanceConfig, ObjectsConfig
from xuanpu_finance.errors import FinanceError

from .test_config import settings

if TYPE_CHECKING:
    from pathlib import Path


async def test_simulated_originals_use_distinct_object_prefix(tmp_path: Path) -> None:
    """即使操作员复用根目录，模拟原件仍位于独立子空间。"""
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    config = ObjectsConfig(kind="local", root=tmp_path / "objects", key_file=key)
    objects = await runtime.objects_from_config(config, simulation=True)
    await objects.put("t/co/object", b"test")
    assert await objects.get("t/co/object") == b"test"
    assert list((tmp_path / "objects" / "simulation").rglob("*.enc"))
    assert not list((tmp_path / "objects" / "t").rglob("*.enc"))


class WrongDatabase:
    """只回报真实数据库名，任何迁移行为都不应发生。"""

    async def fetchval(self, sql: str) -> str:
        """模拟服务器告知这是正式平台数据库，名称不能由DSN推断。"""
        return "platform_production"


async def test_simulation_rejects_wrong_actual_database_before_mutation() -> None:
    """库名门禁在configure/migrate之前，不能仅校验连接字符串。"""
    function = getattr(runtime, "configure_store", None)
    assert function is not None, "缺少运行时真实数据库隔离门禁"
    config = FinanceConfig.model_validate(settings(environment="test", execution_simulation=True))
    with pytest.raises(FinanceError, match="SIMULATION_DATABASE_REQUIRED"):
        await function(WrongDatabase(), config)
