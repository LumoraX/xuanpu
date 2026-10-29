"""资金原凭据共享责任、并发额度与不可复活授权。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

from .test_expense_domain import execute
from .test_f5_domain import draft, policy, preview, setup_funding, submit


async def test_concurrent_cases_share_original_limit_and_withdraw_releases(store: Any) -> None:
    """两单各600共享1000条款，事务竞争最多一单成功。"""
    owner, _, _, _ = await setup_funding(store)
    docs = [await store.create_funding_case(owner, draft(), key) for key in ('one', 'two')]
    plans = [await preview(store, owner, doc) for doc in docs]
    assert all(p['executable'] for p in plans)
    outcomes = await asyncio.gather(*(execute(store, owner, p) for p in plans),
                                    return_exceptions=True)
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert sum(isinstance(o, FinanceError) for o in outcomes) == 1
    rows = await store.list_funding_cases(owner, 'e', status='under_review')
    active = rows['items'][0]
    withdraw = await preview(store, owner, active, 'withdraw', reason='撤回补正')
    assert withdraw['executable'], withdraw['blockers']
    await execute(store, owner, withdraw)
    other = next(d for d in docs if d['id'] != active['id'])
    assert (await submit(store, owner, other))['status'] == 'under_review'


async def test_same_contract_different_terms_or_same_original_renumber_is_conflict(store: Any) -> None:
    """同一合同条款不可通过换编号/换正文取得独立资金额度。"""
    owner, _, _, _ = await setup_funding(store)
    first = draft()
    await store.create_funding_case(owner, first, 'first')
    authority = first.authority.model_dump()
    authority['reference'] = 'renumbered'
    with pytest.raises(FinanceError, match='FUNDING_AUTHORITY_CONFLICT'):
        await store.create_funding_case(owner, draft(authority=authority), 'renumber')
    authority = first.authority.model_dump()
    authority['amount_limit'] = '2000.00'
    with pytest.raises(FinanceError, match='FUNDING_AUTHORITY_CONFLICT'):
        await store.create_funding_case(owner, draft(authority=authority), 'changed')
    assert await store.pool.fetchval('SELECT count(*) FROM finance_funding_authority') == 1


async def test_policy_disable_restore_stales_old_review_but_owner_can_withdraw(store: Any) -> None:
    """禁用再恢复仍增加代际，旧人工批准不能复活。"""
    owner, _, _, approver = await setup_funding(store)
    doc = await submit(store, owner, await store.create_funding_case(owner, draft(), 'c'))
    queue = await store.list_review_queue(approver, 'e')
    decision = await preview(store, approver, doc, 'approve',
                             workflow_step_id=queue['items'][0]['workflow_step_id'])
    await store.configure_funding([policy(enabled=False)])
    await store.configure_funding([policy()])
    assert not (await store.get_action_plan(approver, decision['id']))['executable']
    with pytest.raises(FinanceError):
        await execute(store, approver, decision)
    withdraw = await preview(store, owner, doc, 'withdraw', reason='制度变更')
    assert withdraw['executable'], withdraw['blockers']
    await execute(store, owner, withdraw)


async def test_revoke_authority_requires_human_current_reviewer_cas_and_appends(store: Any) -> None:
    """撤销追加决定并使旧计划失效，申请人不得自行提升审查权。"""
    owner, payer, _, approver = await setup_funding(store)
    doc = await submit(store, owner, await store.create_funding_case(owner, draft(), 'c'))
    authority = doc['funding']['authority']
    with pytest.raises(FinanceError):
        await store.revoke_funding_authority(owner, authority['id'], authority['version'], '撤销', 'owner')
    with pytest.raises(FinanceError, match='HUMAN_REQUIRED'):
        await store.revoke_funding_authority(FinancePrincipal('t', payer.user_id), authority['id'],
                                             authority['version'], '撤销', 'agent')
    revoked = await store.revoke_funding_authority(payer, authority['id'], authority['version'], '原件失效', 'r')
    assert revoked['revoked'] and revoked['version'] == authority['version'] + 1
    assert await store.revoke_funding_authority(payer, authority['id'], authority['version'], '原件失效', 'r') == revoked
    queue = await store.list_review_queue(approver, 'e')
    decision = await preview(store, approver, doc, 'approve',
                             workflow_step_id=queue['items'][0]['workflow_step_id'])
    assert any('AUTHORITY_REVOKED' in x for x in decision['blockers'])
    assert await store.pool.fetchval("SELECT count(*) FROM finance_funding_authority_decision") == 1


async def test_revoked_authority_still_allows_assigned_reject_to_release(store: Any) -> None:
    """撤销阻断新批准，但实际当前审批人仍能驳回释放预留。"""
    owner,payer,_,approver=await setup_funding(store)
    doc=await submit(store,owner,await store.create_funding_case(owner,draft(),'c'))
    authority=doc['funding']['authority']
    await store.revoke_funding_authority(payer,authority['id'],authority['version'],'原件撤销','r')
    step=(await store.list_review_queue(approver,'e'))['items'][0]
    plan=await preview(store,approver,doc,'reject',workflow_step_id=step['workflow_step_id'],reason='原件已撤销')
    assert plan['executable'],plan['blockers']
    await execute(store,approver,plan)
    assert await store.pool.fetchval('SELECT state FROM finance_funding_reservation')=='released'


async def test_authority_expiry_uses_shanghai_day_at_utc_boundary(store: Any, monkeypatch: Any) -> None:
    """UTC尚为前一天时，北京日期已到次日，不能借宿主日期多放行一天。"""
    from datetime import UTC, date, datetime

    import xuanpu_finance.storage.f5_authorities as authority_module

    class FixedClock(datetime):
        """固定UTC边界并保留实际时区转换。"""
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            """上海10月22日00:01对应UTC10月21日16:01。"""
            moment=datetime(2026,10,21,16,1,tzinfo=UTC)
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    class HostDay(date):
        """模拟宿主UTC日历仍在10月21日。"""
        @classmethod
        def today(cls) -> date:
            """宿主日历不能作为中国业务日期。"""
            return date(2026,10,21)

    monkeypatch.setattr(authority_module,'datetime',FixedClock,raising=False)
    monkeypatch.setattr(authority_module,'date',HostDay,raising=False)
    owner,_,_,_=await setup_funding(store)
    body=draft('entrusted_collection')
    spec=body.authority.model_dump()
    spec['valid_until']='2026-10-21'
    doc=await store.create_funding_case(owner,draft('entrusted_collection',authority=spec),'expired')
    plan=await preview(store,owner,doc)
    assert any('FUNDING_AUTHORITY_EXPIRED' in b for b in plan['blockers'])


async def test_employee_original_renumber_and_changed_scope_cannot_duplicate(store: Any) -> None:
    """员工相同实付原件更换交易号或合同不能另起额度责任。"""
    owner,_,_,_=await setup_funding(store)
    body=draft('employee_advance')
    await store.create_funding_case(owner,body,'first')
    spec=body.authority.model_dump()
    spec['reference']='renumbered-payment'
    with pytest.raises(FinanceError,match='FUNDING_AUTHORITY_CONFLICT'):
        await store.create_funding_case(owner,draft('employee_advance',authority=spec),'renumbered')


async def revise_supplier(store: Any, *, contract_version: bool = False) -> dict[str, str]:
    """受控供应商主数据升级版本，稳定企业与供应商ID保持不变。"""
    from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy
    from xuanpu_finance.storage.common import decode
    rows=await store.pool.fetch('SELECT r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.enabled')
    documents,accounts=[],[]
    updated={}
    for row in rows:
        data=decode(row['data'])
        if data['ref']['kind'].endswith('_account'):
            accounts.append(PayeeAccount.model_validate(data))
        else:
            if data['ref']['kind']=='supplier':
                data['ref']={**data['ref'],'version':'v3','sha256':'f'*64}
                updated=data['ref']
            if contract_version and data['ref']['kind']=='contract':
                data['ref']={**data['ref'],'version':'v3','sha256':'e'*64}
            documents.append(BusinessSnapshot.model_validate(data))
    policy=decode(await store.pool.fetchval('SELECT data FROM finance_settlement_policy_revision ORDER BY version DESC LIMIT 1'))
    provider=decode(await store.pool.fetchval('SELECT data FROM finance_settlement_provider ORDER BY configuration_version DESC LIMIT 1'))
    await store.configure_settlements([SettlementPolicy(**policy,enabled=True)],documents,accounts,[{**provider,'enabled':True}],simulation=True)
    return updated


async def test_collection_reference_cannot_reset_authority_across_supplier_versions(store: Any) -> None:
    """供应商同稳定ID升级版本及新扫描件，不能为同授权编号重置责任额度。"""
    from .test_f5_domain import material
    owner,_,_,_=await setup_funding(store)
    first=draft('entrusted_collection')
    await store.create_funding_case(owner,first,'first')
    supplier=await revise_supplier(store)
    await material(store,'new-scan','c'*64)
    spec={**first.authority.model_dump(),'evidence_ids':['new-scan']}
    with pytest.raises(FinanceError,match='FUNDING_AUTHORITY_CONFLICT'):
        await store.create_funding_case(owner,draft('entrusted_collection',supplier_ref=supplier,
            authority=spec,evidence_ids=['new-scan']),'renumber-supplier-version')
    assert await store.pool.fetchval("SELECT count(*) FROM finance_funding_authority WHERE kind='collection_mandate'")==1


async def test_refund_reference_cannot_reset_authority_across_supplier_versions(store: Any) -> None:
    """同供应商退款交易跨原预付与供应商版本也只有一个不可变责任额度。"""
    from .test_f2_domain import provider_result, ref
    from .test_f5_domain import approve, material
    from .test_f5_http_e2e import external_plan, paid_parent
    from .test_f5_reconciliation import refund
    owner,payer,_,approver,parent=await paid_parent(store)
    first_refund=await refund(store,owner,parent,'600.00','first-refund')
    await approve(store,owner,approver,await submit(store,owner,first_refund))
    supplier=await revise_supplier(store,contract_version=True)
    contract={**ref('contract','contract'),'version':'v3','sha256':'e'*64}
    await material(store,'second-parent-material','e'*64)
    terms={**draft().authority.model_dump(),'evidence_ids':['second-parent-material']}
    second_parent=await store.create_funding_case(owner,draft(amount='1000.00',supplier_ref=supplier,business_refs=[contract],
        authority=terms,evidence_ids=['second-parent-material']),'second-parent')
    second_parent=await approve(store,owner,approver,await submit(store,owner,second_parent))
    plan=await external_plan(store,payer,approver,second_parent,'execute_payment',payee_ref=ref('supplier_account','account'))
    await execute(store,payer,plan)
    job=await store.claim_settlement()
    request=await store.prepare_settlement_dispatch(job)
    await store.record_settlement_result(job,provider_result(request),b'second-prepayment-receipt')
    await material(store,'refund-new-scan','a'*64)
    spec={**first_refund['content']['authority'],'evidence_ids':['refund-new-scan']}
    with pytest.raises(FinanceError,match='FUNDING_AUTHORITY_CONFLICT'):
        await store.create_funding_case(owner,draft('prepayment_return',source_case_id=second_parent['id'],
            supplier_ref=supplier,business_refs=[contract],authority=spec,evidence_ids=['refund-new-scan']),'same-refund-new-version')
    assert await store.pool.fetchval("SELECT count(*) FROM finance_funding_authority WHERE kind='return_evidence'")==1


async def test_prepayment_supplier_upgrade_keeps_same_contract_original_responsibility(store: Any) -> None:
    """合同原版本不变时，仅升级供应商主数据不能另起预付条款额度。"""
    from .test_f5_domain import material
    owner,_,_,_=await setup_funding(store)
    await store.create_funding_case(owner,draft(),'first')
    supplier=await revise_supplier(store)
    await material(store,'prepayment-new-scan','c'*64)
    terms={**draft().authority.model_dump(),'evidence_ids':['prepayment-new-scan']}
    with pytest.raises(FinanceError,match='FUNDING_AUTHORITY_CONFLICT'):
        await store.create_funding_case(owner,draft(supplier_ref=supplier,authority=terms,
            evidence_ids=['prepayment-new-scan']),'new-supplier-same-contract')
    assert await store.pool.fetchval("SELECT count(*) FROM finance_funding_authority WHERE kind='prepayment_terms'")==1
