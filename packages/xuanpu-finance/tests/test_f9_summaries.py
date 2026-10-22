"""F9真实PostgreSQL有界摘要的检索、授权和游标验收。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ExpenseDraft
from xuanpu_finance.models import FinancePrincipal

from .test_expense_domain import claim
from .test_f7_records import act, configured, expense_body

if TYPE_CHECKING:
    from xuanpu_finance.storage import FinanceStore

FIELDS={'id','legal_entity_id','owner_id','version','status','total','reason','created_at','record_mode'}


async def test_summary_is_bounded_literal_descending_and_scoped(store: FinanceStore) -> None:
    """字面量大小写检索只查单号/事由，完整粘贴和明细不进入摘要。"""
    owner,_=await configured(store)
    docs=[]
    for i,reason in enumerate(['First 50%_DONE', 'second 50xxDONE', 'third 50%_done']):
        docs.append(await store.create_expense_claim(owner,ExpenseDraft(**expense_body(
            reason=reason,invoice_information='secret needle '*1400)),str(i)))
    first=await store.list_expense_claim_summaries(owner,'e',q='  50%_done ',limit=1)
    assert first['total']==2 and first['items'][0]['id']==docs[2]['id']
    assert set(first['items'][0])==FIELDS and first['items'][0]['total']=='10.00'
    second=await store.list_expense_claim_summaries(owner,'e',q='50%_done',limit=1,cursor=first['next_cursor'])
    assert second['total']==2 and second['items'][0]['id']==docs[0]['id'] and second['next_cursor'] is None
    assert (await store.list_expense_claim_summaries(owner,'e',q='secret needle'))['total']==0
    for changes in [{'q':'other'}, {'view':'visible'}, {'status':'draft'}]:
        args={'q':'50%_done','limit':1,'cursor':first['next_cursor'],**changes}
        with pytest.raises(FinanceError,match='CURSOR_INVALID'):
            await store.list_expense_claim_summaries(owner,'e',**args)
    with pytest.raises(FinanceError):
        await store.list_expense_claim_summaries(FinancePrincipal('other','owner','human'),'e')


async def test_mine_reviewer_and_historical_approver_current_revocation(store: FinanceStore) -> None:
    """本人筛选始终限定owner；历史分派仍须当前岗位和未撤销代数。"""
    owner,people=await configured(store)
    doc=await act(store,owner,await claim(store,owner),'submit_claim')
    reviewer=FinancePrincipal('t','reviewer','human')
    await claim(store,reviewer,key='reviewer')
    assert (await store.list_expense_claim_summaries(reviewer,'e'))['total']==1
    assert (await store.list_expense_claim_summaries(reviewer,'e',view='visible'))['total']==2
    assert (await store.list_expense_claim_summaries(people[0],'e',view='visible'))['items'][0]['id']==doc['id']
    await store.grant('t','a','e','collector');await store.grant('t','a','e','approver')
    assert (await store.list_expense_claim_summaries(people[0],'e',view='visible'))['total']==0
    await store.pool.execute("DELETE FROM finance_grant WHERE user_id='a'")
    with pytest.raises(FinanceError,match='COMPANY_FORBIDDEN'):
        await store.list_expense_claim_summaries(people[0],'e',view='visible')


async def test_fifty_heavily_escaped_summaries_fit_one_mebibyte(store: FinanceStore) -> None:
    """最坏事由转义与长元数据仍有界，原文大小不影响摘要响应。"""
    await configured(store)
    entity='e'*128;owner=FinancePrincipal('t','o'*128,'human')
    await store.seed_company('t',entity,'长标识隔离公司','TEST')
    await store.grant('t',owner.user_id,entity)
    content=json.dumps(expense_body(reason='\x01'*2000,invoice_information='原文'*10000))
    for i in range(50):
        await store.pool.execute(
            "INSERT INTO finance_expense_claim(tenant_id,legal_entity_id,id,owner_id,version,status,total,content,record_mode) VALUES('t',$1,$2,$3,1,'draft',10,$4::jsonb,'internal_record')",
            entity,str(i).zfill(128),owner.user_id,content)
    result=await store.list_expense_claim_summaries(owner,entity,limit=50)
    assert len(result['items'])==50 and result['total']==50 and result['next_cursor'] is None
    assert len(json.dumps(result,ensure_ascii=True).encode())<1048576
    for args in [{'limit':51},{'view':'anything'},{'q':'x'*129},{'status':'paid'}]:
        with pytest.raises(FinanceError):
            await store.list_expense_claim_summaries(owner,entity,**args)
