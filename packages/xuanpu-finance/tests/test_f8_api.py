"""F8真实签名HTTP只读目录与材料元数据边界。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_f0_e2e import SignedClient, app_for
from .test_f8_materials import completed
from .test_storage import WHO
from .test_worker import Gateway

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


async def test_signed_list_detail_params_modes_and_agent_read_only(store: FinanceStore, tmp_path: Path) -> None:
    """参数签名经真实校验，agent只能读元数据，停用拒绝且读取无业务写入。"""
    op, _ = await completed(store, tmp_path)
    key = Ed25519PrivateKey.generate()
    app = app_for(store, EncryptedLocalObjects(tmp_path, b'k' * 32), Gateway({}), key)
    prefix = '/v1/finance/supporting-material-imports'
    before = await store.get_operation(WHO, op['id'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://finance') as http:
        signed = SignedClient(http, key)
        for mode in ['enabled', 'read_only', 'draining']:
            app.state.mode = mode
            response = await signed.call('GET', prefix, params={'legal_entity_id': 'company-a'})
            assert response.status_code == 200, response.text
            assert response.json()['total'] == 1
            detail = await signed.call('GET', prefix + '/' + op['id'])
            assert detail.status_code == 200, detail.text
            assert detail.json()['materials'][0]['selectable'] is True
            assert 'object_key' not in detail.text
        for params in [{}, {'legal_entity_id': ''}, {'legal_entity_id': 'x' * 129},
                       {'legal_entity_id': 'company-a', 'limit': '0'},
                       {'legal_entity_id': 'company-a', 'limit': '101'}]:
            response = await signed.call('GET', prefix, params=params)
            assert response.status_code == 422, response.text
        response = await signed.call('GET', prefix, params={'legal_entity_id': 'company-a', 'cursor': 'bad'})
        assert response.status_code == 400 and response.json()['code'] == 'CURSOR_INVALID'
        app.state.mode = 'disabled'
        response = await signed.call('GET', prefix, params={'legal_entity_id': 'company-a'})
        assert response.status_code == 503 and response.json()['code'] == 'module_disabled'
        assert (await signed.call('GET', prefix + '/' + op['id'])).status_code == 503
        app.state.mode = 'enabled'
        response = await signed.call('GET', prefix + '/missing')
        assert response.status_code == 404
        decision = await signed.call('POST', '/v1/finance/action-executions', body={'plan_id': 'missing', 'plan_digest': '0' * 64}, idem='agent-decision')
        assert decision.status_code == 403, decision.text
    assert await store.get_operation(WHO, op['id']) == before
    assert await store.pool.fetchval('SELECT count(*) FROM finance_execution_grant') == 0
    assert app.title == '玄圃财务 F0–F10' and app.version == '2026.10.23.1'
