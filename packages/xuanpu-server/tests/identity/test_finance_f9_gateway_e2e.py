"""真实员工签名出口验证F9最大摘要不会触发平台响应截断。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.api import create_app as create_finance_app
from xuanpu_finance.expenses.models import ExpenseDraft
from xuanpu_finance.gateway import PlatformGateway
from xuanpu_server.api import create_app
from xuanpu_server.manual_connectors import ManualConnectorService
from xuanpu_server.runtime.call_context import HumanContextSigner

from .test_finance_f7_gateway_e2e import record_people
from .test_finance_gateway_e2e import (
    F2CookieCalls,
    VerifiedFinanceTransport,
    f2_no_model,
    f2_signer_config,
    finance_app,
)
from .test_finance_gateway_e2e import finance_database as _finance_database

if TYPE_CHECKING:
    from xuanpu_finance.storage import FinanceStore

finance_database = _finance_database


async def populate_large_records(store: FinanceStore, reviewer: str) -> str:
    """仅测试夹具构造所有ID为128字符的极限历史记录，读取仍用真实员工。"""
    entity, historical_owner = '\x01' * 128, '\x02' * 128
    await store.seed_company('acme', entity, '有界摘要测试公司', 'TEST')
    await store.grant('acme', reviewer, entity, 'reviewer')
    for position in range(50):
        body = ExpenseDraft(
            legal_entity_id=entity, reason='\x01' * 2000,
            invoice_information='申报' * 10000,
            lines=[{'category_id': 'other', 'amount': '1.00',
                    'occurred_on': '2026-10-22', 'reason': '测试'}],
        )
        await store.pool.execute(
            "INSERT INTO finance_expense_claim(tenant_id,legal_entity_id,id,owner_id,"
            "version,status,total,content,record_mode) "
            "VALUES('acme',$1,$2,$3,1,'draft',1,$4::jsonb,'internal_record')",
            entity, '\x03' * 125 + str(position).zfill(3), historical_owner,
            body.model_dump_json())
    return entity


async def test_maximum_summary_crosses_real_signed_cookie_gateway_without_truncation(
    identity_world: Any, finance_database: FinanceStore, monkeypatch: Any,
) -> None:
    """真实PG/认证/签名/响应边界均参与，ASGI只替代网络传输。"""
    async with identity_world() as ident:
        platform, store = ident.world.platform, finance_database
        people, cookies = await record_people(ident, store)
        entity = await populate_large_records(store, people['accountant'].user_id)
        key, seen = Ed25519PrivateKey.generate(), []
        signing = await f2_signer_config(ident, key)
        monkeypatch.setattr(platform.runtimes, 'get', f2_no_model)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(platform)),
                                     base_url='https://xuanpu.example.com') as online:
            gateway = PlatformGateway(online, tenant_tokens={
                'acme': await ident.system_key(role='tenant_member')})
            baseline = finance_app(store, gateway, key)
            finance = create_finance_app(store=store, auth=baseline.state.auth,
                                         start_worker=False, profile='internal_records')
            platform.manual_connectors = ManualConnectorService(
                packages=platform.packages, vault=platform.vault, audit=platform.audit,
                signer=HumanContextSigner(config=signing, vault=platform.vault),
                transport=VerifiedFinanceTransport(finance, key, seen))
            calls = F2CookieCalls(ident, cookies)
            capabilities = await calls.call('owner', 'getFinanceCapabilities', {})
            assert capabilities['internal_records_only'] is True
            page = await calls.call('accountant', 'listExpenseClaimSummaries', {
                'legal_entity_id': entity, 'view': 'visible', 'limit': 50})
            assert page['total'] == len(page['items']) == 50
            assert page['next_cursor'] is None
            assert len(json.dumps(page, ensure_ascii=True).encode()) < 1048576
            assert all(item['reason'] == '\x01' * 2000 for item in page['items'])
            assert all('content' not in item and len(item) == 9 for item in page['items'])
            assert all(len(item[key]) == 128 for item in page['items']
                       for key in ['id', 'legal_entity_id', 'owner_id'])
            assert (await calls.call('accountant', 'listExpenseClaimSummaries', {
                'legal_entity_id': entity, 'view': 'visible', 'q': '申报'}))['total'] == 0
        assert len(seen) == 3 and all(item['origin'] == 'human' for item in seen)
