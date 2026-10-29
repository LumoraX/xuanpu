"""真实PG运行时在独占后拒绝旧库，内部实例不构造外部执行资源。"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from xuanpu_finance import runtime
from xuanpu_finance.config import FinanceConfig
from xuanpu_finance.errors import FinanceError

from .test_expense_domain import invoice
from .test_f7_records import configured
from .test_f9_profile import internal_settings
from .test_lifecycle import config_for

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


def config(store_schema: str,directory: Path) -> FinanceConfig:
    """明确绑定随机测试schema，保持真实资源生命周期。"""
    parts=urlsplit(os.environ['FINANCE_TEST_DSN'])
    dsn=urlunsplit(parts._replace(query=urlencode(parse_qsl(parts.query)+[('search_path',store_schema)])))
    data=config_for(directory,dsn).model_dump()
    internal=internal_settings()
    for name in ['profile','companies','grants','expense_policies']:
        data[name]=internal[name]
    data['gateway']['tenant_token_files']={'t':directory/'gateway-token'}
    return FinanceConfig.model_validate(data)


async def test_internal_runtime_never_constructs_external_registry_or_worker(store: FinanceStore,tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    """真实启动和重启仅收件、本地通知；供应商注册表、worker与回调均不存在。"""
    cfg=config(await store.pool.fetchval('SELECT current_schema()'),tmp_path/'runtime')
    def forbidden(*args: object,**kwargs: object) -> None:
        """一旦构造任何供应商资源立即失败。"""
        raise AssertionError('internal runtime constructed external resource')
    for name in ['provider_registry','compliance_registry','SettlementWorker','ArchivePipeline']:
        monkeypatch.setattr(runtime,name,forbidden)
    for _ in range(2):
        async with runtime.open_runtime(cfg) as app, app.router.lifespan_context(app):
            assert app.state.profile=='internal_records'
            assert app.state.settlement_worker is None and app.state.compliance_worker is None
            assert app.state.settlement_providers is None and app.state.compliance_providers is None
            assert app.state.settlement_task is None and app.state.compliance_task is None
            assert app.state.worker_task is not None and app.state.notification_task is not None
            assert not any('callback' in path for path in app.openapi()['paths'])


async def test_incompatible_runtime_rejects_before_any_http_objects_or_migration(store: FinanceStore,tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    """持锁后兼容检查先于迁移、配置和任何网关/原件客户端构造。"""
    await configured(store);await invoice(store)
    cfg=config(await store.pool.fetchval('SELECT current_schema()'),tmp_path/'blocked')
    def forbidden(*args: object,**kwargs: object) -> None:
        """不能通过先写后报错声称只读兼容。"""
        raise AssertionError('mutation or HTTP occurred before compatibility check')
    monkeypatch.setattr('httpx.AsyncClient',forbidden)
    monkeypatch.setattr(runtime.FinanceStore,'migrate',forbidden)
    monkeypatch.setattr(runtime,'objects_from_config',forbidden)
    with pytest.raises(FinanceError,match='INTERNAL_DATABASE_INCOMPATIBLE'):
        async with runtime.open_runtime(cfg):
            pytest.fail('旧库不得启动')
    assert await store.pool.fetchval('SELECT count(*) FROM finance_invoice')==1
