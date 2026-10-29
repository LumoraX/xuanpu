"""F9内部记录配置和HTTP装配边界的真实回归。"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from xuanpu_finance.api import create_app
from xuanpu_finance.config import FinanceConfig

from .test_config import settings
from .test_expense_domain import policy


def internal_settings(**changes: Any) -> dict[str, Any]:
    """构造独立试用公司，提交人与审批人分开。"""
    return settings(profile='internal_records', companies=[{
        'tenant_id':'t','legal_entity_id':'e','name':'隔离测试公司','tax_id':'TEST'}],
        grants=[{'tenant_id':'t','legal_entity_id':'e','user_id':u,'role':r}
                for u,r in [('owner','collector'),('a','approver'),('reviewer','reviewer')]],
        expense_policies=[policy().model_copy(update={'record_mode':'internal_record'}).model_dump()],
        **changes)


def test_internal_config_and_general_default() -> None:
    """内部模式可显式启用，旧通用配置保持兼容。"""
    assert FinanceConfig.model_validate(settings()).profile == 'general'
    assert FinanceConfig.model_validate(internal_settings()).profile == 'internal_records'


@pytest.mark.parametrize('changes',[
    {'companies':[], 'grants':[], 'expense_policies':[]}, {'expense_policies':[]},
    {'expense_policies':[policy().model_dump()]},
    {'expense_policies':[policy(enabled=False).model_copy(update={'record_mode':'internal_record'}).model_dump()]},
    {'environment':'test','synthetic':True}, {'execution_simulation':True},
    {'grants':[{'tenant_id':'t','legal_entity_id':'e','user_id':u,'role':r}
               for u,r in [('a','approver'),('reviewer','reviewer')]]},
    {'gateway':{'base_url':'https://example.invalid','tenant_token_files':{'t':'secret'}}},
    {'issuer':'REPLACE_ISSUER'},
])
def test_internal_config_rejects_unusable_settings(changes: dict[str,Any]) -> None:
    """未填模板、空制度、合成模拟与缺少独立申请人均失败关闭。"""
    value=internal_settings();value.update(changes)
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(value)


def test_internal_app_has_only_explicit_allowlist() -> None:
    """内部实例不注册通用业务或回调，profile也不能随意拼写。"""
    app=create_app(store=None,auth=None,start_worker=False,profile='internal_records')
    paths=app.openapi()['paths']
    ids={v.get('operationId') for methods in paths.values() for v in methods.values()}
    assert 'getFinanceCapabilities' in ids and 'listExpenseClaimSummaries' in ids
    assert 'importInvoices' not in ids and 'listIntegrations' not in ids
    assert not any('callback' in path for path in paths)
    with pytest.raises(ValueError):
        create_app(store=None,auth=None,profile='typo')


def test_internal_app_rejects_synthetic_worker() -> None:
    """应用工厂不能通过直接注入绕过内部禁止合成材料的边界。"""
    from xuanpu_finance.invoices.worker import FinanceWorker

    worker=FinanceWorker(None,None,None,synthetic=True)
    with pytest.raises(ValueError):
        create_app(store=None,auth=None,worker=worker,profile='internal_records')
