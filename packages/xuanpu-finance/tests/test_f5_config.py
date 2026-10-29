"""资金制度必须显式配置且版本和公司岗位不可伪造。"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.settlement.provider_models import ProviderTarget

from .test_f5_domain import SCENARIOS, models, policy, setup_funding


def test_policy_requires_all_explicit_nonempty_scenario_chains() -> None:
    """禁止空审批、自造场景和省略场景获得默认放行。"""
    for steps in ({}, {'prepayment': []}, {s: [] for s in SCENARIOS}):
        with pytest.raises(ValidationError):
            policy(steps=steps)
    assert models().FundingPolicy.model_fields.keys() == {
        'tenant_id', 'legal_entity_id', 'version', 'enabled', 'steps'}


def test_provider_target_explicit_funding_capability_contract() -> None:
    """通道目标承认专用单据，但缺显式能力仍不具有发送资格。"""
    assert ProviderTarget(id='funding', type='funding_case', version=1).type == 'funding_case'


async def test_funding_policy_same_version_is_immutable_and_missing_role_rejected(store: Any) -> None:
    """操作员不能用同版本替换正文或配置不存在的审批人。"""
    await setup_funding(store)
    steps = {s: [{'id': 'a', 'name': 'changed', 'approver_user_id': 'a'}] for s in SCENARIOS}
    with pytest.raises(FinanceError, match='POLICY_VERSION_CONFLICT'):
        await store.configure_funding([policy(steps=steps)])
    steps = {s: [{'id': 'unknown', 'name': 'unknown', 'approver_user_id': 'unknown'}] for s in SCENARIOS}
    with pytest.raises(FinanceError, match='CONFIGURATION_INVALID'):
        await store.configure_funding([policy(version=2, steps=steps)])


@pytest.mark.parametrize('explicit_empty',[False,True])
async def test_old_provider_empty_funding_capability_keeps_persisted_restart_digest(
    store: Any,tmp_path: Any,explicit_empty: bool,
) -> None:
    """旧F2配置经当前模型重启不变；空能力不能成为同版本覆盖或新场景资格。"""
    from xuanpu_finance.operations import request_digest
    from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy
    from xuanpu_finance.settlement.provider_models import ProviderConfig
    from xuanpu_finance.storage.common import decode

    from .test_f2_domain import configured
    await configured(store)
    previous=await store.pool.fetchrow('SELECT * FROM finance_settlement_provider')
    old=decode(previous['data'])
    assert 'supported_funding_scenarios' not in old
    config=ProviderConfig(**old,enabled=True,token_file=tmp_path/'token',
        callback_secret_file=tmp_path/'callback',callback_key_id='key',
        **({'supported_funding_scenarios':[]} if explicit_empty else {}))
    descriptor=config.public_descriptor()
    assert request_digest({k:v for k,v in descriptor.items() if k!='enabled'})==previous['digest']
    rows=await store.pool.fetch('SELECT r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.enabled')
    documents,accounts=[],[]
    for row in rows:
        data=decode(row['data'])
        if data['ref']['kind'].endswith('_account'):
            accounts.append(PayeeAccount.model_validate(data))
        else:
            documents.append(BusinessSnapshot.model_validate(data))
    raw=decode(await store.pool.fetchval('SELECT data FROM finance_settlement_policy_revision'))
    policy=SettlementPolicy(**raw,enabled=True)
    await store.configure_settlements([policy],documents,accounts,[descriptor],simulation=True)
    nonempty=config.model_copy(update={'supported_funding_scenarios':['prepayment']})
    with pytest.raises(FinanceError,match='PROVIDER_VERSION_CONFLICT'):
        await store.configure_settlements([policy],documents,accounts,[nonempty.public_descriptor()],simulation=True)
    fresh=nonempty.model_copy(update={'configuration_version':2})
    fresh_policy=policy.model_copy(update={'version':2,'payment_configuration_version':2,'posting_configuration_version':2})
    await store.configure_settlements([fresh_policy],documents,accounts,[fresh.public_descriptor()],simulation=True)
    assert decode(await store.pool.fetchval('SELECT data FROM finance_settlement_provider WHERE configuration_version=2'))['supported_funding_scenarios']==['prepayment']


def test_compliance_config_rejects_funding_payment_capability(tmp_path: Any) -> None:
    """F3继承公共配置不能接受仅属于付款记账通道的资金能力声明。"""
    from xuanpu_finance.compliance.protocol import ComplianceProviderConfig
    with pytest.raises(ValidationError,match='资金'):
        ComplianceProviderConfig(tenant_id='t',legal_entity_id='e',provider='archive',
            configuration_version=1,adapter_version='http-v1',base_url='http://127.0.0.1:1',
            token_file=tmp_path/'token',callback_secret_file=tmp_path/'callback',callback_key_id='key',
            simulation=True,supports_idempotency=True,supports_query=True,kind='archive',
            supports_object_versions=True,supports_retention_readback=True,
            supported_funding_scenarios=['prepayment'])
