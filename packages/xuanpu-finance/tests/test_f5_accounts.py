"""F5建单前脱敏账户选择及按实际资金场景过滤的账户目录。"""
from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy
from xuanpu_finance.storage.common import decode

from .test_f2_domain import ref
from .test_f5_domain import draft, setup_funding
from .test_f5_http_e2e import paid_parent


async def add_visibility_accounts(store: Any) -> None:
    """通过当前受控配置加入不可见、未核实和其他员工的实际目录项。"""
    rows=await store.pool.fetch('SELECT r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.enabled')
    documents,accounts=[],[]
    for row in rows:
        data=decode(row['data'])
        if data['ref']['kind'].endswith('_account'):
            accounts.append(PayeeAccount.model_validate(data))
        else:
            documents.append(BusinessSnapshot.model_validate(data))
    for kind,id,subject,verified,allowed in [
        ('employee_account','other-employee','other',True,['owner']),
        ('supplier_account','unverified','supplier',False,['owner']),
        ('supplier_account','private-account','another-supplier',True,['payer']),
        ('supplier_account','second-supplier-account','supplier',True,['owner','payer']),
    ]:
        accounts.append(PayeeAccount(tenant_id='t',legal_entity_id='e',ref=ref(kind,id),
            subject_id=subject,account_name=id,masked_account='****9876',provider_account_id='secret-'+id,
            verified=verified,allowed_user_ids=allowed))
    policy=decode(await store.pool.fetchval('SELECT data FROM finance_settlement_policy_revision ORDER BY version DESC LIMIT 1'))
    provider=decode(await store.pool.fetchval('SELECT data FROM finance_settlement_provider ORDER BY configuration_version DESC LIMIT 1'))
    await store.configure_settlements([SettlementPolicy(**policy,enabled=True)],documents,accounts,[{**provider,'enabled':True}],simulation=True)


async def test_funding_options_accounts_allow_initial_binding_without_document(store: Any) -> None:
    """建单前只返回当前本人可用的受控脱敏账户，不能泄漏其他员工或渠道令牌。"""
    owner,payer,_,approver=await setup_funding(store)
    await add_visibility_accounts(store)
    options=await store.get_funding_options(owner,'e')
    assert 'accounts' in options, '资金建单前缺少可绑定账户选择'
    accounts=options['accounts']
    assert {a['ref']['id'] for a in accounts}=={'account','employee','entrusted','second-supplier-account'}
    assert all(set(a)=={'ref','account_name','masked_account','verified','subject_id'} for a in accounts)
    assert all(a['verified'] for a in accounts)
    payer_options=await store.get_funding_options(payer,'e')
    assert all(a['ref']['kind']=='supplier_account' for a in payer_options['accounts'])
    assert (await store.get_funding_options(approver,'e'))['accounts']==[]


@pytest.mark.parametrize('scenario,expected',[
    ('prepayment',{'account','second-supplier-account'}),
    ('employee_advance',{'employee'}),
    ('entrusted_collection',{'entrusted'}),
])
async def test_case_payee_accounts_follow_exact_funding_scenario(store: Any,scenario: str,expected: set[str]) -> None:
    """预付保持供应商本人，员工/委托只呈现原声明绑定账户，原经办可继续。"""
    owner,payer,_,_=await setup_funding(store)
    await add_visibility_accounts(store)
    authority=draft(scenario).authority.model_dump()
    if scenario=='entrusted_collection':
        authority['account_ref']=ref('supplier_account','entrusted')
    doc=await store.create_funding_case(owner,draft(scenario,authority=authority),'c')
    for actor in (owner,payer):
        page=await store.list_payee_accounts(actor,'e','funding_case',doc['id'],limit=1)
        assert page['total']==len(expected)
        ids={a['ref']['id'] for a in page['items']}
        if page['next_cursor']:
            page=await store.list_payee_accounts(actor,'e','funding_case',doc['id'],cursor=page['next_cursor'],limit=1)
            ids.update(a['ref']['id'] for a in page['items'])
        assert ids==expected
        assert all(set(a)=={'ref','account_name','masked_account','verified'} for a in page['items'])
    with pytest.raises(FinanceError,match='RESOURCE_NOT_FOUND'):
        await store.list_payee_accounts(owner,'other-company','funding_case',doc['id'])


@pytest.mark.parametrize('scenario',['prepayment_clearance','prepayment_return'])
async def test_closing_cases_never_offer_payment_accounts(store: Any,scenario: str) -> None:
    """两种收尾虽有原供应商和退款材料，也不得呈现再次付款账户。"""
    owner,payer,_,_,parent=await paid_parent(store)
    doc=await store.create_funding_case(owner,draft(scenario,source_case_id=parent['id']),'child')
    for actor in (owner,payer):
        result=await store.list_payee_accounts(actor,'e','funding_case',doc['id'])
        assert result['items']==[] and result['total']==0 and result['next_cursor'] is None
