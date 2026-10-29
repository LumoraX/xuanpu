"""F5三种付款、记账及未知恢复使用真实loopback HTTP渠道。"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionPreview, AllocationRequest
from xuanpu_finance.settlement.provider_models import ProviderConfig
from xuanpu_finance.settlement.providers import ProviderRegistry
from xuanpu_finance.settlement.worker import SettlementWorker

from .test_expense_domain import execute, invoice
from .test_f2_domain import provider_result, ref
from .test_f2_http_e2e import provider_server
from .test_f5_domain import (
    approve,
    draft,
    funding_catalog,
    material,
    preview,
    setup_funding,
    submit,
)
from .test_worker import Gateway


async def allocated_funding(store: Any, owner: Any, scenario: str, amount: str = '600.00') -> dict[str, Any]:
    """完整发票分配连到已保全原件，不用纯数据库票面冒充实际原件。"""
    authority = None
    if scenario == 'entrusted_collection':
        authority = draft(scenario).authority.model_dump()
        authority['account_ref'] = ref('supplier_account', 'entrusted')
    doc = await store.create_funding_case(owner, draft(scenario, amount, **({'authority':authority} if authority else {})), 'create-'+scenario)
    await invoice(store, id='invoice-'+scenario)
    await store.pool.execute("UPDATE finance_invoice SET source_simulation=true WHERE id=$1", 'invoice-'+scenario)
    original = 'original-'+scenario
    await material(store, original, 'c'*64)
    await store.pool.execute("UPDATE finance_item SET state='parsed' WHERE operation_id=$1", original)
    await store.pool.execute("UPDATE finance_operation SET purpose='invoice' WHERE id=$1", original)
    await store.pool.execute("INSERT INTO finance_invoice_receipt VALUES('t','e',$1,$2)", 'invoice-'+scenario, original)
    await store.create_invoice_allocation(owner, AllocationRequest(
        invoice_id='invoice-'+scenario, invoice_version=1, document_id=doc['id'],
        document_kind='funding_case', expected_document_version=doc['version'],
        amount=amount, tax='0.00'), 'allocate-'+scenario)
    return await store.get_funding_case(owner, doc['id'])


async def external_plan(store: Any, who: Any, approver: Any, doc: Any, action: str,
                         **parameters: Any) -> dict[str, Any]:
    """原经办提出、实际分派者批准同一固定计划，原经办再明确执行。"""
    plan = await preview(store, who, doc, action, amount=doc['total'], **parameters)
    assert plan['blockers'] == ['ACTION_APPROVAL_REQUIRED:该固定行动计划尚需专用人工批准'], plan['blockers']
    item = next(i for i in (await store.list_review_queue(approver, 'e'))['items'] if i['target_id']==plan['id'])
    review = await store.preview_finance_action(approver, ActionPreview(
        action='approve',target_type='action_plan',target_id=plan['id'],expected_version=1,
        parameters={'workflow_step_id':item['workflow_step_id']}), 'approve-'+plan['id'])
    assert review['executable'], review['blockers']
    await execute(store, approver, review)
    ready = await store.get_action_plan(who, plan['id'])
    assert ready['executable'], ready['blockers']
    return ready


async def enable_provider(store: Any, *, origin: str = 'http://127.0.0.1:1', tmp_path: Any = None) -> ProviderConfig:
    """显式新版本渠道声明五种场景，旧渠道默认空能力不变。"""
    from pathlib import Path
    folder = tmp_path or Path('/tmp')
    config = ProviderConfig(tenant_id='t',legal_entity_id='e',provider='test',
        configuration_version=2,adapter_version='http-v1',base_url=origin,
        token_file=folder/'token',callback_secret_file=folder/'callback',callback_key_id='key',
        simulation=True,supports_idempotency=True,supports_query=True,
        supported_funding_scenarios=['prepayment','employee_advance','entrusted_collection',
                                    'prepayment_clearance','prepayment_return'])
    await funding_catalog(store, provider=config.public_descriptor())
    return config


@pytest.mark.parametrize('scenario,account', [('prepayment','account'), ('employee_advance','employee'),
                                             ('entrusted_collection','entrusted')])
async def test_three_funding_scenarios_actual_http_payment_and_posting(store: Any, tmp_path: Any,
                                                                      scenario: str, account: str) -> None:
    """真实HTTP固定收款主体和业务依据，已发未知重启只GET且原计划单POST。"""
    owner,payer,accountant,approver = await setup_funding(store)
    async with provider_server() as (server,origin):
        config = await enable_provider(store, origin=origin, tmp_path=tmp_path)
        doc = (await store.create_funding_case(owner,draft(),'create') if scenario=='prepayment'
               else await allocated_funding(store,owner,scenario))
        doc = await approve(store,owner,approver,await submit(store,owner,doc))
        kind = 'employee_account' if scenario=='employee_advance' else 'supplier_account'
        payment = await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref(kind,account))
        operation = await execute(store,payer,payment)
        assert (await execute(store,payer,payment,'another-key'))['id']==operation['id']
        async with httpx.AsyncClient(trust_env=False) as client:
            registry = ProviderRegistry([config],client,tokens={('test',2):'isolated-token'},callback_secrets={('test',2):'s'*32})
            worker = SettlementWorker(store,registry,Gateway({}))
            assert await worker.run_once()
            assert len(server.posts)==1
            request = server.posts[0]
            assert request['funding']['scenario']==scenario
            assert request['target']['type']=='funding_case'
            assert request['provider_account_id']==('provider-account' if account=='account' else account)
            await store.pool.execute("UPDATE finance_settlement_operation SET status='outcome_unknown',next_query_at=now()-interval '1 second'")
            assert await SettlementWorker(store,registry,Gateway({})).run_once(allow_dispatch=False)
            assert len(server.posts)==1 and len(server.queries)==1
            assert (await store.get_funding_case(owner,doc['id']))['paid_amount']=='600.00'
            posting = await external_plan(store,accountant,approver,doc,'submit_posting',accounting_period='2026-10')
            await execute(store,accountant,posting)
            assert await worker.run_once()
            await store.pool.execute("UPDATE finance_settlement_operation SET next_query_at=now()-interval '1 second' WHERE status='awaiting_provider'")
            assert (await store.get_funding_case(owner,doc['id']))['posted_amount']=='600.00'
            assert len(server.posts)==2
            assert server.posts[1]['funding']['accounting_basis']==('prepayment_basis' if scenario=='prepayment' else 'invoice')


async def test_old_provider_does_not_implicitly_support_prepayment(store: Any) -> None:
    """原F2渠道能力默认不包含任何F5资金场景。"""
    owner,payer,_,approver = await setup_funding(store)
    doc = await approve(store,owner,approver,await submit(store,owner,await store.create_funding_case(owner,draft(),'c')))
    plan = await preview(store,payer,doc,'execute_payment',amount='600.00',payee_ref=ref('supplier_account','account'))
    assert any('FUNDING_PROVIDER_CAPABILITY_REQUIRED' in b for b in plan['blockers'])
    assert await store.pool.fetchval('SELECT count(*) FROM finance_settlement_operation')==0


async def paid_parent(store: Any, amount: str = '1000.00') -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    """实际F2固定请求及确定渠道交易后才成为收尾父单。"""
    owner,payer,accountant,approver = await setup_funding(store)
    await enable_provider(store)
    doc = await approve(store,owner,approver,await submit(store,owner,await store.create_funding_case(owner,draft(amount=amount),'parent')))
    plan = await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref('supplier_account','account'))
    await execute(store,payer,plan)
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    await store.record_settlement_result(job,provider_result(request),b'actual-test-channel-receipt')
    return owner,payer,accountant,approver,await store.get_funding_case(owner,doc['id'])


async def test_employee_submit_requires_every_actual_original_and_cannot_pay_supplier(store: Any) -> None:
    """员工垫付完整分配仍不能跳过实际发票原件，收款只准本人账户。"""
    owner,payer,_,approver=await setup_funding(store)
    await enable_provider(store)
    doc=await allocated_funding(store,owner,'employee_advance')
    await store.pool.execute("UPDATE finance_item SET state='quarantined' WHERE operation_id='original-employee_advance'")
    plan=await preview(store,owner,doc)
    assert any('FUNDING_EVIDENCE_INVALID' in b for b in plan['blockers'])
    await store.pool.execute("UPDATE finance_item SET state='parsed' WHERE operation_id='original-employee_advance'")
    doc=await approve(store,owner,approver,await submit(store,owner,doc))
    bad=await preview(store,payer,doc,'execute_payment',amount='600.00',payee_ref=ref('supplier_account','account'))
    assert any('PAYEE_MISMATCH' in b for b in bad['blockers'])


async def test_revoke_after_accept_blocks_dispatch_but_sent_unknown_still_original_get(store: Any) -> None:
    """最后发送窗口重审授权；已发未知不被撤销改写成可再次POST。"""
    owner,payer,_,approver=await setup_funding(store)
    await enable_provider(store)
    doc=await approve(store,owner,approver,await submit(store,owner,await store.create_funding_case(owner,draft(),'c')))
    plan=await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref('supplier_account','account'))
    await execute(store,payer,plan)
    authority=doc['funding']['authority']
    await store.revoke_funding_authority(payer,authority['id'],authority['version'],'撤销','r')
    job=await store.claim_settlement()
    with pytest.raises(FinanceError,match='FUNDING_AUTHORITY_REVOKED'):
        await store.prepare_settlement_dispatch(job)
    assert await store.pool.fetchval('SELECT request FROM finance_settlement_operation') is None


async def test_already_sent_unknown_ignores_revocation_for_original_query_only(store: Any) -> None:
    """已发未知后撤权不能丢失资金事实，恢复仍GET原请求并禁止第二个POST。"""
    owner,payer,_,approver=await setup_funding(store)
    await enable_provider(store)
    doc=await approve(store,owner,approver,await submit(store,owner,await store.create_funding_case(owner,draft(),'c')))
    plan=await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref('supplier_account','account'))
    await execute(store,payer,plan)
    job=await store.claim_settlement()
    request=await store.prepare_settlement_dispatch(job)
    await store.defer_settlement(job,'HTTP_TIMEOUT',uncertain=True)
    await store.release_settlement(job)
    authority=doc['funding']['authority']
    await store.revoke_funding_authority(payer,authority['id'],authority['version'],'已发后发现原件问题','r')
    await store.pool.execute("UPDATE finance_settlement_operation SET next_query_at=now()-interval '1 second'")
    recovered=await store.claim_settlement(allow_dispatch=False)
    assert recovered['mode']=='query' and recovered['request']==request
    await store.record_settlement_result(recovered,provider_result(request),b'reconciled-original-request')
    assert (await store.get_funding_case(owner,doc['id']))['paid_amount']=='600.00'
    assert await store.pool.fetchval('SELECT count(*) FROM finance_settlement_operation')==1
    assert not (await store.get_action_plan(payer,plan['id']))['executable']


@pytest.mark.parametrize('change',['funding_policy','material','account'])
async def test_last_send_window_rechecks_current_policy_material_account(store: Any, change: str) -> None:
    """操作受理不等于发送许可，发送前制度代际、材料和账户必须仍匹配。"""
    from .test_f5_domain import policy
    owner,payer,_,approver=await setup_funding(store)
    await enable_provider(store)
    doc=await approve(store,owner,approver,await submit(store,owner,await store.create_funding_case(owner,draft(),'c')))
    plan=await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref('supplier_account','account'))
    await execute(store,payer,plan)
    if change=='funding_policy':
        await store.configure_funding([policy(enabled=False)])
        await store.configure_funding([policy()])
    elif change=='material':
        await store.pool.execute("UPDATE finance_item SET state='quarantined'")
    else:
        await store.pool.execute("UPDATE finance_settlement_catalog SET generation=generation+1 WHERE kind='supplier_account' AND id='account'")
    job=await store.claim_settlement()
    with pytest.raises(FinanceError):
        await store.prepare_settlement_dispatch(job)
    assert await store.pool.fetchval('SELECT request FROM finance_settlement_operation') is None
    assert (await store.get_funding_case(owner,doc['id']))['payment_pending_amount']=='600.00'
