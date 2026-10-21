"""F5专用API严格契约、主体签名与生命周期不放宽已有人工边界。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .test_f0_e2e import SignedClient, app_for
from .test_f5_domain import draft, setup_funding, submit
from .test_worker import Gateway

if TYPE_CHECKING:
    from pathlib import Path


async def test_signed_funding_crud_and_authority_revoke_require_real_reviewer(store: Any,tmp_path: Path) -> None:
    """资金单据独立入口、签名版本和撤销真人权限形成可恢复的真实记录。"""
    from xuanpu_finance.objects import EncryptedLocalObjects
    owner,_,_,_=await setup_funding(store)
    key=Ed25519PrivateKey.generate();app=app_for(store,EncryptedLocalObjects(tmp_path,b'k'*32),Gateway({}),key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as client:
        signed=SignedClient(client,key)
        from .f3_contract_checks import PublishedResponses
        contracts=PublishedResponses()
        async def call(method: str,path: str,user: str='owner',**kwargs: Any) -> httpx.Response:
            """每次真实签名固定租户及角色，不靠请求正文声称身份。"""
            response=await signed.call(method,path,tenant='t',user=user,**kwargs)
            if response.is_success and "/catalog/" not in path:
                contracts.validate(method,path,response.status_code,response.json())
            return response
        response=await call('POST','/v1/finance/funding-cases',body=draft().model_dump(mode='json'),idem='case')
        assert response.status_code==201,response.text
        case=response.json();assert case['kind']=='funding_case'
        listed=await call('GET','/v1/finance/funding-cases',params={'legal_entity_id':'e','limit':'1'})
        assert listed.status_code==200,listed.text
        assert listed.json()['total']==1 and listed.json()['items'][0]['id']==case['id']
        accounts=await call('GET','/v1/finance/catalog/payee-accounts',params={'legal_entity_id':'e','document_kind':'funding_case','document_id':case['id']})
        assert accounts.status_code==200,accounts.text
        options=await call('GET','/v1/finance/funding-options',params={'legal_entity_id':'e'})
        assert options.json()['enabled'] is True and len(options.json()['scenarios'])==5
        forged=await call('POST','/v1/finance/funding-cases',body={**draft().model_dump(mode='json'),'owner_id':'payer'},idem='forged')
        assert forged.status_code==422
        update=await call('PUT','/v1/finance/funding-cases/'+case['id'],body={'expected_version':case['version'],'draft':draft(reason='修订资金用途').model_dump(mode='json')},idem='update')
        assert update.status_code==200,update.text
        authority=update.json()['funding']['authority']
        path='/v1/finance/funding-authorities/'+authority['id']+'/revoke'
        body={'expected_version':authority['version'],'reason':'授权材料需要重新核对'}
        agent=await call('POST',path,user='payer',origin='agent',body=body,idem='agent')
        assert agent.status_code==403
        applicant=await call('POST',path,body=body,idem='owner')
        assert applicant.status_code==403
        revoked=await call('POST',path,user='payer',origin='human',body=body,idem='revoke')
        assert revoked.status_code==200 and revoked.json()['revoked'],revoked.text
        replay=await call('POST',path,user='payer',origin='human',body=body,idem='revoke')
        assert replay.json()==revoked.json()
        assert (await store.get_funding_case(owner,case['id']))['funding']['authority']['revoked']


async def test_draining_does_not_classify_new_funding_approval_as_cancellation(store: Any,tmp_path: Path) -> None:
    """旧approve的cancel标签不能使排空中的资金新批准取得执行资格。"""
    from xuanpu_finance.objects import EncryptedLocalObjects
    owner,_,_,approver=await setup_funding(store)
    case=await submit(store,owner,await store.create_funding_case(owner,draft(),'case'))
    step=case['workflow']['current_step_id']
    key=Ed25519PrivateKey.generate();app=app_for(store,EncryptedLocalObjects(tmp_path,b'k'*32),Gateway({}),key)
    app.state.mode='draining'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as client:
        signed=SignedClient(client,key)
        response=await signed.call('POST','/v1/finance/action-previews',tenant='t',user=approver.user_id,origin='human',body={'action':'approve','target_type':'funding_case','target_id':case['id'],'expected_version':case['version'],'parameters':{'workflow_step_id':step}},idem='approve-draining')
        assert response.status_code==409,response.text
        assert (await store.get_funding_case(owner,case['id']))['status']=='under_review'
