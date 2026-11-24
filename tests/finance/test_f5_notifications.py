"""专用资金审批提醒使用真实目标、当前资金制度及岗位，不读取旧批准。"""
from __future__ import annotations

from typing import Any

from .test_f5_domain import draft, policy, setup_funding, submit


async def test_funding_approval_notice_uses_current_funding_policy_and_kind(store: Any) -> None:
    """资金制度版本与报销不同仍提醒正确原目标；关闭资金制度后旧提醒退出。"""
    owner,_,_,approver=await setup_funding(store)
    await store.configure_funding([policy(version=2)])
    doc=await store.create_funding_case(owner,draft(),'case')
    doc=await submit(store,owner,doc)
    await store.refresh_finance_notifications()
    page=await store.list_finance_notifications(approver,'e')
    items=[n for n in page['items'] if n['kind']=='approval_pending']
    assert len(items)==1 and items[0]['target_type']=='funding_case'
    assert items[0]['target_id']==doc['id']
    await store.configure_funding([policy(version=3,enabled=False)])
    assert (await store.list_finance_notifications(approver,'e'))['total']==0


async def test_overdue_prepayment_notice_tracks_real_unsettled_balance(store: Any) -> None:
    """已确定付款且到票期限已过才提醒；全额内部退回核对完成后退出当前提醒。"""
    from .test_expense_domain import execute
    from .test_f2_domain import ref
    from .test_f5_domain import approve
    from .test_f5_http_e2e import enable_provider, external_plan, provider_result
    from .test_f5_reconciliation import refund
    owner,payer,_,approver=await setup_funding(store)
    await enable_provider(store)
    doc=await store.create_funding_case(owner,draft(amount='1000.00',due_date='2020-01-01'),'overdue')
    doc=await approve(store,owner,approver,await submit(store,owner,doc))
    await store.refresh_finance_notifications()
    assert not [n for n in (await store.list_finance_notifications(owner,'e'))['items'] if n['kind']=='funding_due']
    plan=await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref('supplier_account','account'))
    await execute(store,payer,plan)
    job=await store.claim_settlement();request=await store.prepare_settlement_dispatch(job)
    await store.record_settlement_result(job,provider_result(request),b'isolated-channel-receipt')
    await store.refresh_finance_notifications()
    notices=[n for n in (await store.list_finance_notifications(owner,'e'))['items'] if n['kind']=='funding_due']
    assert len(notices)==1 and notices[0]['target_type']=='funding_case'
    await approve(store,owner,approver,await submit(store,owner,await refund(store,owner,doc,'1000.00','returned')))
    assert not [n for n in (await store.list_finance_notifications(owner,'e'))['items'] if n['kind']=='funding_due']
