"""F9签名HTTP验证摘要范围、模式、能力和共用旧计划拒绝。"""
from __future__ import annotations

from importlib.metadata import version
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.api import create_app
from xuanpu_finance.expenses.models import ActionPreview, ExpenseDraft
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_f0_e2e import SignedClient, app_for
from .test_f2_domain import configured as payable_configured
from .test_f2_domain import payable
from .test_f7_records import configured, expense_body
from .test_worker import Gateway

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


def internal_app(store: FinanceStore,tmp_path: Path,key: Ed25519PrivateKey,mode: str='enabled') -> Any:
    """复用真实签名鉴权，内部应用仅注入必要原件资源。"""
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    base=app_for(store,objects,Gateway({}),key)
    return create_app(store=store,auth=base.state.auth,objects=objects,start_worker=False,
                      profile='internal_records',mode=mode)


async def test_capabilities_auth_exact_projection_and_summary_http(store: FinanceStore,tmp_path: Path) -> None:
    """能力仍鉴权；签名摘要检索字面匹配、有界且当前权限生效。"""
    owner,_=await configured(store)
    for i in range(3):
        await store.create_expense_claim(owner,ExpenseDraft(**expense_body(reason=f'费用{i} 50%_TEST',invoice_information='秘密'*10000)),str(i))
    key=Ed25519PrivateKey.generate();app=internal_app(store,tmp_path,key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as http:
        signed=SignedClient(http,key)
        path='/v1/finance/expense-claim-summaries'
        assert (await http.get('/v1/finance/capabilities')).status_code==401
        cap=await signed.call('GET','/v1/finance/capabilities',tenant='t',user='owner')
        assert cap.json()=={'profile':'internal_records','mode':'enabled','service_version':version('xuanpu-finance'),'internal_records_only':True}
        response=await signed.call('GET',path,tenant='t',user='owner',params={'legal_entity_id':'e','q':'50%_test','limit':'1'})
        assert response.status_code==200,response.text
        assert response.json()['total']==3 and '秘密' not in response.text
        for params in [{'limit':'51'},{'q':'x'*129},{'view':'all'},{'status':'paid'},{}]:
            result=await signed.call('GET',path,tenant='t',user='owner',params=params)
            assert result.status_code==422,result.text
        for forbidden in ['/v1/finance/invoice-imports','/v1/finance/integrations','/v1/finance/funding-cases','/v1/finance/invoices']:
            assert (await signed.call('GET',forbidden,tenant='t',user='owner')).status_code==404
        await store.pool.execute("DELETE FROM finance_grant WHERE tenant_id='t' AND user_id='owner'")
        result=await signed.call('GET',path,tenant='t',user='owner',params={'legal_entity_id':'e','cursor':response.json()['next_cursor']})
        assert result.status_code==403


@pytest.mark.parametrize('mode,read_status,write_status',[('read_only',200,409),('disabled',503,503),('draining',200,409)])
async def test_internal_profile_preserves_module_modes(store: FinanceStore,tmp_path: Path,mode: str,read_status: int,write_status: int) -> None:
    """profile不替代只读、停用和排空语义。"""
    await configured(store)
    key=Ed25519PrivateKey.generate();app=internal_app(store,tmp_path,key,mode)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as http:
        signed=SignedClient(http,key)
        read=await signed.call('GET','/v1/finance/expense-claim-summaries',tenant='t',user='owner',params={'legal_entity_id':'e'})
        assert read.status_code==read_status,read.text
        write=await signed.call('POST','/v1/finance/expense-claims',tenant='t',user='owner',body=expense_body(),idem=mode)
        assert write.status_code==write_status,write.text


async def test_internal_shared_endpoints_refuse_actual_old_payable_plan(store: FinanceStore,tmp_path: Path) -> None:
    """通用模式留下的应付计划不能由共用get/execute穿透内部白名单。"""
    owner,_,_,_=await payable_configured(store)
    doc=await payable(store,owner)
    plan=await store.preview_finance_action(owner,ActionPreview(action='submit_payable',target_type='payable',target_id=doc['id'],expected_version=doc['version'],parameters={}),'old-plan')
    key=Ed25519PrivateKey.generate();app=internal_app(store,tmp_path,key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as http:
        signed=SignedClient(http,key)
        calls=[('GET','/v1/finance/action-plans/'+plan['id'],None),
               ('POST','/v1/finance/action-executions',{'plan_id':plan['id'],'plan_digest':plan['digest']}),
               ('POST','/v1/finance/action-previews',{'action':'submit_payable','target_type':'payable','target_id':doc['id'],'expected_version':doc['version'],'parameters':{}})]
        for i,(method,path,body) in enumerate(calls):
            result=await signed.call(method,path,tenant='t',user=owner.user_id,origin='human',body=body,idem='blocked'+str(i))
            assert result.status_code==403,result.text
            assert result.json()['code']=='INTERNAL_RECORDS_ONLY'
    assert (await store.get_payable(owner,doc['id']))['status']=='draft'


async def test_internal_preview_checks_actual_target_not_claimed_type(store: FinanceStore,tmp_path: Path) -> None:
    """声明expense_claim不能为实际应付单生成内部计划。"""
    owner,_,_,_=await payable_configured(store)
    doc=await payable(store,owner)
    key=Ed25519PrivateKey.generate();app=internal_app(store,tmp_path,key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as http:
        signed=SignedClient(http,key)
        response=await signed.call('POST','/v1/finance/action-previews',tenant='t',user=owner.user_id,origin='human',idem='mislabelled',body={
            'action':'submit_claim','target_type':'expense_claim','target_id':doc['id'],
            'expected_version':doc['version'],'parameters':{}})
        assert response.status_code in {403,404},response.text
    assert await store.pool.fetchval('SELECT count(*) FROM finance_action_plan')==0
