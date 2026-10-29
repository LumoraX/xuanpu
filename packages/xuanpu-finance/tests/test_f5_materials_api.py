"""签名支持材料入口保持真实员工、严格请求和独立持久收件。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .test_f0_e2e import SignedClient, app_for
from .test_storage import WHO, prepare
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


async def test_signed_material_api_rejects_invoice_supplement_and_saves_real_pdf(store: FinanceStore,tmp_path: Path) -> None:
    """固定签名API只接材料字段，后台保存真实PDF而非创建发票或批准事实。"""
    from xuanpu_finance.objects import EncryptedLocalObjects
    await prepare(store)
    raw=b'%PDF-1.4 authorization paper'
    files=[ref('material',raw).model_dump()]
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    key=Ed25519PrivateKey.generate()
    app=app_for(store,objects,Gateway({'material':raw}),key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as client:
        call=SignedClient(client,key)
        response=await call.call('POST','/v1/finance/supporting-material-imports',tenant=WHO.tenant_id,user=WHO.user_id,body={'legal_entity_id':'company-a','files':files},idem='material')
        assert response.status_code==202,response.text
        op=response.json()
        assert await app.state.worker.run_once()
        result=await call.call('GET','/v1/finance/operations/'+op['id'],tenant=WHO.tenant_id,user=WHO.user_id)
        assert result.json()['summary']['materials']==1
        forbidden=await call.call('POST','/v1/finance/supporting-material-imports',tenant=WHO.tenant_id,user=WHO.user_id,body={'legal_entity_id':'company-a','files':files,'source_exception_id':'forged','expected_exception_version':1},idem='forged')
        assert forbidden.status_code==422
        assert await store.pool.fetchval('SELECT count(*) FROM finance_invoice')==0
