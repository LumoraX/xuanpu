"""当前有权审批人能实际核对独立原件，撤权后不能继续读取分块。"""
from __future__ import annotations

import base64
import importlib
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

from .test_f5_archive import real_funding_source
from .test_f5_domain import submit

if TYPE_CHECKING:
    from pathlib import Path


async def test_original_bytes_current_assignment_and_epoch(store: Any,tmp_path: Path) -> None:
    """原平台可离线，当前分派人按固定摘要读取原件，旧epoch恢复不能读旧单据。"""
    owner,tax,approver,doc,objects,raw=await real_funding_source(store,tmp_path)
    assert importlib.util.find_spec('xuanpu_finance.funding.materials') is not None,'缺少HITL真实材料读取'
    read=importlib.import_module('xuanpu_finance.funding.materials').read_material
    receipt=doc['content']['evidence_ids'][0]
    auditor=FinancePrincipal('t','a','human')
    with pytest.raises(FinanceError):
        await read(store,objects,auditor,doc['id'],receipt,0,128)
    await submit(store,owner,doc)
    chunks=[];offset=0
    while offset is not None:
        result=await read(store,objects,auditor,doc['id'],receipt,offset,128)
        chunks.append(base64.b64decode(result['content_base64']));offset=result['next_offset']
    assert b''.join(chunks)==raw and result['media_type']=='application/pdf'
    await store.grant('t','a','e','collector');await store.grant('t','a','e','approver')
    with pytest.raises(FinanceError):
        await read(store,objects,auditor,doc['id'],receipt,0,128)
    assert base64.b64decode((await read(store,objects,tax,doc['id'],receipt,0,len(raw)))['content_base64'])==raw
    with pytest.raises(FinanceError):
        await read(store,objects,owner,doc['id'],'other-receipt',0,128)
    with pytest.raises(FinanceError):
        await read(store,objects,FinancePrincipal('other','owner','human'),doc['id'],receipt,0,128)


async def test_signed_material_read_matches_published_response(store: Any,tmp_path: Path) -> None:
    """真实签名GET分块受当前读权约束，公开契约不暴露内部对象路径。"""
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from .f3_contract_checks import PublishedResponses
    from .test_f0_e2e import SignedClient, app_for
    from .test_worker import Gateway
    _,_,_,doc,objects,raw=await real_funding_source(store,tmp_path)
    key=Ed25519PrivateKey.generate();app=app_for(store,objects,Gateway({}),key)
    path='/v1/finance/funding-cases/'+doc['id']+'/materials/'+doc['content']['evidence_ids'][0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as http:
        signed=SignedClient(http,key)
        response=await signed.call('GET',path,tenant='t',user='owner',params={'limit':'128'})
        assert response.status_code==200,response.text
        PublishedResponses().validate('GET',path,response.status_code,response.json())
        assert base64.b64decode(response.json()['content_base64'])==raw[:128]
        assert 'object_key' not in response.text
