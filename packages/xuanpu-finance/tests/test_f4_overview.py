"""运营计数与接入状态必须来自实际独立财务事实。"""
from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError

from .test_f3_domain import configured, invoice


async def test_integrations_reflect_configured_simulation_and_internal_approval(store: Any) -> None:
    """F3隔离通道和内部审批不能继续固定显示未配置。"""
    owner,tax,_=await configured(store)
    items={item['kind']:item for item in (await store.list_integrations(owner,'e'))['items']}
    assert items['tax']['status']=='configured_simulation'
    assert items['archive']['status']=='configured_simulation'
    assert items['approval']['status']=='configured_internal'
    assert items['payment']['status']=='unconfigured'
    assert 'token' not in str(items) and 'http://' not in str(items)


async def test_overview_counts_real_scoped_queues_and_rejects_collector(store: Any) -> None:
    """当前reviewer可见独立聚合，collector没有公司运营数据权限。"""
    owner,tax,_=await configured(store)
    await invoice(store)
    op=await store.verify_invoice(owner,'blue',1,'v')
    await store.seed_company('t','other','另一公司','OTHER')
    overview=await store.get_finance_overview(tax,'e')
    assert overview['legal_entity_id']=='e'
    assert overview['queues']['compliance']['status_counts']=={'accepted':1}
    assert overview['queues']['compliance']['oldest_created_at']
    assert op['id'] not in str(overview) and 'request_template' not in str(overview)
    with pytest.raises(FinanceError,match='REVIEWER_REQUIRED'):
        await store.get_finance_overview(owner,'e')
    with pytest.raises(FinanceError):
        await store.get_finance_overview(tax,'other')


async def test_old_completed_operation_does_not_age_current_queue(store: Any) -> None:
    """早期终态只计历史数量，最早积压取真正仍待处理的操作。"""
    from .test_f3_domain import fresh_snapshot, result_for
    owner,tax,_=await configured(store)
    await invoice(store)
    first=await store.verify_invoice(owner,'blue',1,'old')
    job=await store.claim_compliance();await result_for(store,job,fresh_snapshot())
    await store.pool.execute("UPDATE finance_compliance_operation SET created_at=now()-interval '10 years' WHERE id=$1",first['id'])
    second=await store.verify_invoice(owner,'blue',2,'new')
    created=await store.pool.fetchval('SELECT created_at FROM finance_compliance_operation WHERE id=$1',second['id'])
    overview=await store.get_finance_overview(tax,'e')
    assert overview['queues']['compliance']['oldest_created_at']==created.isoformat()
    assert overview['queues']['compliance']['status_counts']=={'accepted':1,'succeeded':1}


async def test_overview_excludes_revoked_recipients_old_unread_notices(store: Any) -> None:
    """当前无人可读的旧审批提醒不能永久算作无法清理的未读积压。"""
    from xuanpu_finance.models import FinancePrincipal

    from .test_expense_domain import invoice as expense_invoice
    from .test_expense_domain import setup, submitted
    owner,approvers=await setup(store)
    await expense_invoice(store);await submitted(store,owner)
    await store.refresh_finance_notifications()
    operator=FinancePrincipal('t','operator','human');await store.grant('t','operator','e','reviewer')
    await store.grant('t',approvers[0].user_id,'e','collector')
    expected=sum([(await store.list_finance_notifications(who,'e'))['unread_total'] for who in [owner,*approvers,operator]])
    overview=await store.get_finance_overview(operator,'e')
    assert overview['notifications']['unread']==expected
