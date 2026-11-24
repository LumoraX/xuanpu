"""执行追踪配置：默认关闭、空名单不推导全开、拼错的键报错。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.engine.tracing.config import ExecutionTraceConfig


def test_defaults_are_closed() -> None:
    """不配置时任何租户都不采集。"""
    config = ExecutionTraceConfig()
    assert not config.allows("acme")
    assert (config.max_spans_per_trace, config.retention_days) == (2048, 90)


def test_enabled_with_empty_list_stays_closed() -> None:
    """开关打开但名单为空，仍不开启。"""
    assert not ExecutionTraceConfig(enabled=True).allows("acme")


def test_only_listed_tenants() -> None:
    """显式名单与开关同时满足才开启。"""
    config = ExecutionTraceConfig(enabled=True, tenants=("acme",))
    assert config.allows("acme") and not config.allows("globex")
    assert not ExecutionTraceConfig(enabled=False, tenants=("acme",)).allows("acme")


@pytest.mark.parametrize("bad", [
    {"enabled": "yes"}, {"max_spans_per_trace": 1}, {"retention_days": 0}, {"typo": 1},
])
def test_invalid_values_rejected(bad: dict[str, object]) -> None:
    """非布尔开关、越界上限和未知键都报错。"""
    with pytest.raises(ValidationError):
        ExecutionTraceConfig.model_validate(bad)
