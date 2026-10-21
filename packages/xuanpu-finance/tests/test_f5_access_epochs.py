"""资金历史指派不得在撤权恢复后复活单据或资金事实读取权限。"""
from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError

from .test_expense_domain import execute
from .test_f2_domain import provider_result
from .test_f5_domain import draft, preview, setup_funding, submit
from .test_f5_http_e2e import external_plan, paid_parent


async def rotate_approver(store: Any,approver: Any) -> None:
    """经过collector撤权再授approver，使原步骤epoch保持1而当前为3。"""
    await store.grant('t',approver.user_id,'e','collector')
    await store.grant('t',approver.user_id,'e','approver')
    assert await store.pool.fetchval("SELECT generation FROM finance_grant_epoch WHERE tenant_id='t' AND legal_entity_id='e' AND user_id=$1",approver.user_id)==3


@pytest.mark.parametrize('reading',['get','list'])
async def test_funding_old_assignment_does_not_revive_after_regrant(store: Any,reading: str) -> None:
    """历史步骤不等于当前授权，原申请人与当前reviewer仍保留合法读取。"""
    owner,reviewer,_,approver=await setup_funding(store)
    doc=await submit(store,owner,await store.create_funding_case(owner,draft(),'c'))
    assert (await store.get_funding_case(approver,doc['id']))['id']==doc['id']
    await rotate_approver(store,approver)
    assert (await store.list_review_queue(approver,'e'))['total']==0
    if reading=='get':
        with pytest.raises(FinanceError,match='RESOURCE_NOT_FOUND'):
            await store.get_funding_case(approver,doc['id'])
    else:
        page=await store.list_funding_cases(approver,'e')
        assert page['total']==0 and page['items']==[]
    for who in (owner,reviewer):
        assert (await store.get_funding_case(who,doc['id']))['id']==doc['id']
        assert (await store.list_funding_cases(who,'e'))['total']==1
    withdraw=await preview(store,owner,doc,'withdraw',reason='重新分派当前岗位')
    await execute(store,owner,withdraw)
    current=await store.get_funding_case(owner,doc['id'])
    await submit(store,owner,current)
    assert (await store.get_funding_case(approver,doc['id']))['id']==doc['id']
    assert (await store.list_funding_cases(approver,'e'))['total']==1


@pytest.mark.parametrize('reading',['get_payment','get_posting','list_payments','list_postings','get_action_plan'])
async def test_funding_external_facts_and_plan_hide_revoked_historical_assignment(store: Any,reading: str) -> None:
    """未传document_id的批量接口与具体资金/计划接口都不得绕过F5当前代数。"""
    owner,payer,accountant,approver,doc=await paid_parent(store)
    posting=await external_plan(store,accountant,approver,doc,'submit_posting',accounting_period='2026-10')
    await execute(store,accountant,posting)
    job=await store.claim_settlement()
    request=await store.prepare_settlement_dispatch(job)
    await store.record_settlement_result(job,provider_result(request),b'posting-proof')
    payment=(await store.list_payments(owner,'e'))['items'][0]
    voucher=(await store.list_postings(owner,'e'))['items'][0]
    await rotate_approver(store,approver)
    if reading.startswith('list_'):
        page=await getattr(store,reading)(approver,'e')
        assert page['total']==0 and page['items']==[]
        assert (await getattr(store,reading)(owner,'e'))['total']==1
        assert (await getattr(store,reading)(payer,'e'))['total']==1
    else:
        id={'get_payment':payment['id'],'get_posting':voucher['id'],'get_action_plan':posting['id']}[reading]
        with pytest.raises(FinanceError,match='RESOURCE_NOT_FOUND'):
            await getattr(store,reading)(approver,id)


@pytest.mark.parametrize('reading',['get_invoice','list_invoice_lines','list_invoice_fields',
                                   'get_invoice_evidence','get_invoice_compliance','list_invoices'])
async def test_funding_invoice_association_does_not_revive_old_approver_epoch(store: Any,reading: str) -> None:
    """资金关联票据及原件、字段、合规快照读取同样不得借历史步骤恢复。"""
    from .test_f5_http_e2e import allocated_funding
    owner,reviewer,_,approver=await setup_funding(store)
    doc=await submit(store,owner,await allocated_funding(store,owner,'employee_advance'))
    invoice_id='invoice-employee_advance'
    assert (await store.get_invoice(approver,invoice_id))['id']==invoice_id
    await rotate_approver(store,approver)
    if reading=='list_invoices':
        page=await store.list_invoices(approver,'e')
        assert page['total']==0 and page['items']==[]
        assert (await store.list_invoices(owner,'e'))['total']==1
        assert (await store.list_invoices(reviewer,'e'))['total']==1
    else:
        with pytest.raises(FinanceError,match='RESOURCE_NOT_FOUND'):
            await getattr(store,reading)(approver,invoice_id)
    assert (await store.get_funding_case(owner,doc['id']))['id']==doc['id']
