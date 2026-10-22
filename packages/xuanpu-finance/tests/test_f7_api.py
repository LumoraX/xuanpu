"""F7真实签名HTTP与独立PostgreSQL、原件字节的端到端边界。"""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .test_f0_e2e import SignedClient, app_for
from .test_f7_records import expense_body, original_doc
from .test_worker import Gateway

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


async def test_signed_f7_record_history_material_and_strict_input(store: FinanceStore,tmp_path: Path) -> None:
    """签名入口实际读取原件并拒绝agent、伪造状态、无存储及越界参数。"""
    _,_,doc,objects,raw,receipt=await original_doc(store,tmp_path)
    key=Ed25519PrivateKey.generate();app=app_for(store,objects,Gateway({}),key)
    prefix='/v1/finance/expense-claims';path=prefix+'/'+doc['id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as http:
        signed=SignedClient(http,key)
        async def call(method: str,url: str,**kwargs: Any) -> httpx.Response:
            """所有请求经真实签名和当前身份核验。"""
            return await signed.call(method,url,tenant='t',user='owner',origin='human',**kwargs)
        response=await call('POST',prefix,body=expense_body(invoice_information='  原文\n '),idem='signed-paste')
        assert response.status_code==201,response.text
        assert response.json()['content']['invoice_information']=='  原文\n '
        for text in ['', ' \t ', 'x'*20001]:
            result=await call('POST',prefix,body=expense_body(invoice_information=text),idem='invalid'+str(len(text)))
            assert result.status_code==422,result.text
        result=await call('POST',prefix,body=expense_body(invoice_information='票号',verified=True),idem='verified')
        assert result.status_code==422
        history=await call('GET',path+'/history',params={'limit':'1'})
        assert history.status_code==200,history.text
        assert history.json()['total']==1 and history.json()['items'][0]['kind']=='revision'
        material=path+'/materials/'+receipt
        result=await call('GET',material,params={'limit':'128','document_version':'1'})
        assert result.status_code==200,result.text
        assert base64.b64decode(result.json()['content_base64'])==raw[:128]
        assert 'object_key' not in result.text
        agent=await signed.call('GET',material,tenant='t',user='owner',origin='agent')
        assert agent.status_code==403,agent.text
        for params in [{'offset':'-1'},{'limit':'262145'},{'document_version':'0'}]:
            result=await call('GET',material,params=params)
            assert result.status_code==422,result.text
        app.state.objects=None
        unavailable=await call('GET',material)
        assert unavailable.status_code==503 and unavailable.json()['code']=='MATERIAL_STORE_UNAVAILABLE'
    assert app.title=='玄圃财务 F0–F8' and app.version=='2026.10.22.5'


async def test_signed_invoice_information_nul_is_422_for_create_and_revision(store: FinanceStore,tmp_path: Path) -> None:
    """数据库JSONB不能接收NUL，创建和修订均须在签名API校验时422拒绝。"""
    owner,_,doc,objects,_,_=await original_doc(store,tmp_path)
    key=Ed25519PrivateKey.generate();app=app_for(store,objects,Gateway({}),key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app,raise_app_exceptions=False),base_url='https://finance') as http:
        signed=SignedClient(http,key)
        draft=expense_body(invoice_information='发票\x00信息')
        for method,path,body in [
            ('POST','/v1/finance/expense-claims',draft),
            ('PUT','/v1/finance/expense-claims/'+doc['id'],{'expected_version':doc['version'],'draft':draft}),
        ]:
            result=await signed.call(method,path,tenant='t',user='owner',origin='human',body=body,idem='nul-'+method)
            assert result.status_code==422,result.text
    saved=await store.get_expense_claim(owner,doc['id'])
    assert saved['version']==doc['version']
