"""已付预付与核销/退款子单共享余额及最终事实不可回退。"""
from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError

from .test_f5_domain import draft, setup_funding


async def test_clearance_requires_paid_same_company_supplier_contract_parent(store: Any) -> None:
    """未知或未付预付不可核销，父匹配检查在建单时拒绝。"""
    owner, _, _, _ = await setup_funding(store)
    parent = await store.create_funding_case(owner, draft(), 'parent')
    with pytest.raises(FinanceError, match='PREPAYMENT_PAYMENT_REQUIRED'):
        await store.create_funding_case(owner, draft('prepayment_clearance', source_case_id=parent['id']), 'child')


async def clearance(store: Any, owner: Any, parent: Any, amount: str, key: str) -> dict[str, Any]:
    """按子单实际金额提议完整发票分配。"""
    from xuanpu_finance.expenses.models import AllocationRequest

    from .test_expense_domain import invoice
    from .test_f5_domain import material
    doc = await store.create_funding_case(owner, draft('prepayment_clearance',amount,
                                                       source_case_id=parent['id']),key)
    await invoice(store, key, gross=amount, tax='0.00')
    await store.pool.execute('UPDATE finance_invoice SET source_simulation=true WHERE id=$1',key)
    original='original-'+key
    await material(store,original,'e'*64)
    await store.pool.execute("UPDATE finance_item SET state='parsed' WHERE operation_id=$1",original)
    await store.pool.execute("INSERT INTO finance_invoice_receipt VALUES('t','e',$1,$2)",key,original)
    await store.create_invoice_allocation(owner,AllocationRequest(invoice_id=key,invoice_version=1,
        document_id=doc['id'],document_kind='funding_case',expected_document_version=doc['version'],
        amount=amount,tax='0.00'),'allocation-'+key)
    return await store.get_funding_case(owner,doc['id'])


async def refund(store: Any, owner: Any, parent: Any, amount: str, key: str) -> dict[str, Any]:
    """退款必须明确实际原交易、日期、金额和独立支持原件。"""
    from .test_f5_domain import material
    await material(store,key,'d'*64)
    authority = draft('prepayment_return',source_case_id=parent['id']).authority.model_dump()
    authority.update(evidence_ids=[key],reference='refund-transaction')
    return await store.create_funding_case(owner,draft('prepayment_return',amount,
        source_case_id=parent['id'],authority=authority,evidence_ids=[key]),key)


async def test_partial_clearance_return_share_balance_and_final_facts_are_immutable(store: Any) -> None:
    """核销400与退回300后未结300，最终事实及consumed票据不得回退。"""
    from xuanpu_finance.funding.models import FundingDraft, FundingUpdate

    from .test_expense_domain import execute
    from .test_f5_domain import approve, preview, submit
    from .test_f5_http_e2e import paid_parent
    owner,payer,_,approver,parent = await paid_parent(store)
    clear = await approve(store,owner,approver,await submit(store,owner,
        await clearance(store,owner,parent,'400.00','clear')))
    returned = await approve(store,owner,approver,await submit(store,owner,
        await refund(store,owner,parent,'300.00','refund')))
    assert clear['funding']['cleared_amount']=='400.00' and clear['funding']['outstanding_amount']=='0.00'
    assert returned['funding']['returned_amount']=='300.00'
    summary = (await store.get_funding_case(owner,parent['id']))['funding']
    assert summary['cleared_amount']=='400.00' and summary['returned_amount']=='300.00'
    assert summary['outstanding_amount']=='300.00'
    assert await store.pool.fetchval("SELECT state FROM finance_allocation WHERE document_id=$1",clear['id'])=='consumed'
    for child in (clear,returned):
        assert child['funding']['internal_attestation']['source']=='finance_decision'
        withdraw=await preview(store,owner,child,'withdraw',reason='不允许回退')
        assert any('FUNDING_FINALIZED' in b for b in withdraw['blockers'])
        with pytest.raises(FinanceError):
            await execute(store,owner,withdraw)
        with pytest.raises(FinanceError):
            await store.update_funding_case(owner,child['id'],FundingUpdate(
                expected_version=child['version'],draft=FundingDraft(**child['content'])),'revise-'+child['id'])
        pay=await preview(store,payer,child,'execute_payment',amount=child['total'],
                           payee_ref={'kind':'supplier_account','source':'controlled','id':'account','version':'v1','sha256':'a'*64})
        assert any('FUNDING_PAYMENT_FORBIDDEN' in b for b in pay['blockers'])
    history = await store.pool.fetchval('SELECT data FROM finance_expense_history WHERE id=$1 AND version=$2',clear['id'],clear['version'])
    from xuanpu_finance.storage.common import decode
    # 已完成子单历史必须含实际完成后父余额维度及最终实际决定。
    assert decode(history)['funding']['internal_attestation']['source']=='finance_decision'
    assert decode(history)['funding']['financial_version']==clear['funding']['financial_version']
    assert await store.pool.fetchval("SELECT count(*) FROM finance_payment")==1


async def test_clearance_and_refund_concurrent_submit_cannot_exceed_paid_parent(store: Any) -> None:
    """两种子单各600并发竞争1000，失败无票据或责任额度半提交。"""
    import asyncio

    from .test_expense_domain import execute
    from .test_f5_domain import preview
    from .test_f5_http_e2e import paid_parent
    owner,_,_,_,parent = await paid_parent(store)
    children = [await clearance(store,owner,parent,'600.00','clear'),
                await refund(store,owner,parent,'600.00','refund')]
    plans = [await preview(store,owner,c) for c in children]
    assert all(p['executable'] for p in plans)
    outcomes=await asyncio.gather(*(execute(store,owner,p) for p in plans),return_exceptions=True)
    assert sum(isinstance(x,dict) for x in outcomes)==1
    assert sum(isinstance(x,FinanceError) for x in outcomes)==1
    summary=(await store.get_funding_case(owner,parent['id']))['funding']
    assert sum(map(__import__('decimal').Decimal,[summary['clearance_pending_amount'],summary['return_pending_amount']]))==600


async def test_reviewer_cannot_create_another_owners_closing_family(store: Any) -> None:
    """reviewer读权不能借建子单泄露他人材料至原申请人归档闭包。"""
    from .test_f5_http_e2e import paid_parent
    owner,payer,_,_,parent=await paid_parent(store)
    with pytest.raises(FinanceError):
        await store.create_funding_case(payer,draft('prepayment_clearance','100.00',source_case_id=parent['id']),'other-owner')


async def test_finalized_reservation_is_immutable_at_database_boundary(store: Any) -> None:
    """已完成核销责任不能被直接更新或删除静默恢复原预付额度。"""
    import asyncpg

    from .test_f5_domain import approve, submit
    from .test_f5_http_e2e import paid_parent
    owner,_,_,approver,parent=await paid_parent(store)
    child=await approve(store,owner,approver,await submit(store,owner,
        await clearance(store,owner,parent,'400.00','clear')))
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await store.pool.execute("UPDATE finance_funding_reservation SET state='released' WHERE document_id=$1",child['id'])
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await store.pool.execute('DELETE FROM finance_funding_reservation WHERE document_id=$1',child['id'])


async def test_withdraw_child_releases_parent_and_invoice_before_history(store: Any) -> None:
    """撤回收尾历史必须反映已释放的双侧额度和当前资金版本。"""
    from xuanpu_finance.storage.common import decode

    from .test_expense_domain import execute
    from .test_f5_domain import preview, submit
    from .test_f5_http_e2e import paid_parent
    owner,_,_,_,parent=await paid_parent(store)
    child=await submit(store,owner,await clearance(store,owner,parent,'400.00','clear'))
    plan=await preview(store,owner,child,'withdraw',reason='重新补正')
    await execute(store,owner,plan)
    current=await store.get_funding_case(owner,child['id'])
    history=decode(await store.pool.fetchval('SELECT data FROM finance_expense_history WHERE id=$1 AND version=$2',child['id'],current['version']))
    assert history['funding']['clearance_pending_amount']=='0.00'
    assert history['funding']['financial_version']==current['funding']['financial_version']
    assert (await store.get_funding_case(owner,parent['id']))['funding']['clearance_pending_amount']=='0.00'
    assert await store.pool.fetchval("SELECT state FROM finance_allocation WHERE document_id=$1",child['id'])=='released'


async def test_finalized_clearance_can_post_after_original_review_deadline(store: Any) -> None:
    """最终核销不能回退，审批截止日经过后仍可由新专用计划完成记账。"""
    from .test_f5_domain import approve, submit
    from .test_f5_http_e2e import external_plan, paid_parent
    owner,_,accountant,approver,parent=await paid_parent(store)
    child=await approve(store,owner,approver,await submit(store,owner,
        await clearance(store,owner,parent,'400.00','clear')))
    await store.pool.execute("UPDATE finance_workflow SET due_at=now()-interval '1 second' WHERE target_type='funding_case' AND document_id=$1",child['id'])
    posting=await external_plan(store,accountant,approver,child,'submit_posting',accounting_period='2026-10')
    assert posting['executable']


async def test_closing_new_material_cannot_hide_invalid_parent_original(store: Any) -> None:
    """子单采用另一材料时也必须复核原预付实际原件责任，不得只看父ID。"""
    from .test_f5_domain import material
    from .test_f5_http_e2e import paid_parent
    owner,_,_,_,parent=await paid_parent(store)
    await material(store,'child-material','f'*64)
    await store.pool.execute("UPDATE finance_item SET state='quarantined' WHERE operation_id='material'")
    with pytest.raises(FinanceError,match='FUNDING_EVIDENCE_INVALID'):
        await store.create_funding_case(owner,draft('prepayment_clearance','100.00',
            source_case_id=parent['id'],evidence_ids=['child-material']),'hide-parent')
