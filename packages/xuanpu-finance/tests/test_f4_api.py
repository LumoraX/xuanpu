"""真实签名API读取原生通知、人工已读和reviewer运营概览。"""
from __future__ import annotations

from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_f0_e2e import SignedClient, app_for
from .test_f3_domain import configured
from .test_package import validate_schema
from .test_worker import Gateway, ref


async def test_actual_signed_notification_read_and_overview(store: Any,tmp_path: Any) -> None:
    """agent只查，已读须human且不代表审批；概览拒绝collector。"""
    await configured(store)
    raw=b'%PDF-1.4 original requiring structured source'
    gateway=Gateway({'missing':raw});key=Ed25519PrivateKey.generate()
    app=app_for(store,EncryptedLocalObjects(tmp_path/'objects',b'k'*32),gateway,key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://finance') as client:
        signed=SignedClient(client,key)
        response=await signed.call('POST','/v1/finance/invoice-imports',tenant='t',user='owner',origin='human',body={'legal_entity_id':'e','files':[ref('missing',raw).model_dump()]},idem='import')
        assert response.status_code==202,response.text
        await app.state.worker.run_once();await store.refresh_finance_notifications()
        result=await signed.call('GET','/v1/finance/notifications',tenant='t',user='owner',origin='agent',params={'legal_entity_id':'e'})
        assert result.status_code==200,result.text
        validate_schema('FinanceNotificationPage',result.json())
        notice=result.json()['items'][0]
        assert result.json()['unread_total']>=1 and notice['target_type']=='exception'
        path=f"/v1/finance/notifications/{notice['id']}/read"
        blocked=await signed.call('POST',path,tenant='t',user='owner',origin='agent',body={'expected_version':notice['version']},idem='agent-read')
        assert blocked.status_code==403,blocked.text
        read=await signed.call('POST',path,tenant='t',user='owner',origin='human',body={'expected_version':notice['version']},idem='human-read')
        assert read.status_code==200 and read.json()['read_at'],read.text
        repeated=await signed.call('POST',path,tenant='t',user='owner',origin='human',body={'expected_version':notice['version']},idem='human-read')
        validate_schema('FinanceNotification',read.json())
        assert repeated.json()==read.json()
        rejected=await signed.call('GET','/v1/finance/overview',tenant='t',user='owner',origin='human',params={'legal_entity_id':'e'})
        assert rejected.status_code==403
        overview=await signed.call('GET','/v1/finance/overview',tenant='t',user='tax',origin='agent',params={'legal_entity_id':'e'})
        assert overview.status_code==200 and overview.json()['queues']['imports']['status_counts']=={'failed':1},overview.text
        assert 'runtime' in overview.json()
        validate_schema('FinanceOverview',overview.json())
