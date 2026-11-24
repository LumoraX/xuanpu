"""定期清理：按配置的保留天数删除过期 trace，未开启时不起后台任务，单租户失败不影响其余。"""

from __future__ import annotations

from datetime import timedelta

from server.engine.tracing.config import ExecutionTraceConfig
from server.engine.tracing.memory import MemorySpanStore
from server.engine.tracing.retention import TraceRetention

from .helpers import ALICE, OTHER_TENANT_ALICE, T0

DAY = 86400.0


async def test_purges_only_expired_traces_of_enabled_tenants() -> None:
    """保留 1 天：两天前的删掉，今天的留下；未开启租户的不动。"""
    store = MemorySpanStore()
    await store.open_trace(ALICE, trace_id="trc_old", origin="user", title="t", at=T0)
    await store.open_trace(ALICE, trace_id="trc_new", origin="user", title="t",
                           at=T0 + timedelta(days=2))
    await store.open_trace(OTHER_TENANT_ALICE, trace_id="trc_other", origin="user", title="t", at=T0)
    config = ExecutionTraceConfig(enabled=True, tenants=("acme",), retention_days=1)
    now = (T0 + timedelta(days=2, hours=1)).timestamp()
    assert await TraceRetention(store, config, now=lambda: now).run_once() == 1
    assert [t.trace_id for t in (await store.list(ALICE)).items] == ["trc_new"]
    assert (await store.list(OTHER_TENANT_ALICE)).total == 1


class _Broken(MemorySpanStore):
    """某个租户的清理总是失败。"""

    async def purge(self, tenant_id: str, **kwargs: object) -> int:  # type: ignore[override]
        if tenant_id == "acme":
            raise ConnectionError("down")
        return await super().purge(tenant_id, **kwargs)  # type: ignore[arg-type]


async def test_one_tenant_failing_does_not_stop_others() -> None:
    """失败只记日志。"""
    store = _Broken()
    await store.open_trace(OTHER_TENANT_ALICE, trace_id="trc_g", origin="user", title="t", at=T0)
    config = ExecutionTraceConfig(enabled=True, tenants=("acme", "globex"), retention_days=1)
    assert await TraceRetention(store, config, now=lambda: T0.timestamp() + 3 * DAY).run_once() == 1


async def test_disabled_config_starts_nothing() -> None:
    """未开启时不起后台任务，关闭也不报错。"""
    retention = TraceRetention(MemorySpanStore(), ExecutionTraceConfig(), now=lambda: 0.0)
    retention.start()
    assert retention._task is None  # noqa: SLF001
    await retention.close()
