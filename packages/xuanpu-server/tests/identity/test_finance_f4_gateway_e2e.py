"""真实cookie、CSRF和人工签名在模型关闭时处理财务原生提醒。"""
from __future__ import annotations

from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.compliance.provider import ComplianceRegistry
from xuanpu_finance.compliance.queue import ComplianceQueueAdapter
from xuanpu_finance.gateway import PlatformGateway
from xuanpu_finance.settlement.worker import SettlementWorker
from xuanpu_server.api import create_app
from xuanpu_server.manual_connectors import ManualConnectorService
from xuanpu_server.runtime.call_context import HumanContextSigner

from .test_finance_f3_gateway_e2e import prepare_finance, tax_response
from .test_finance_gateway_e2e import (
    F2CookieCalls,
    VerifiedFinanceTransport,
    f2_no_model,
    f2_people_and_expenses,
    f2_signer_config,
    finance_app,
)
from .test_finance_gateway_e2e import finance_database as _finance_database

finance_database = _finance_database


async def test_cookie_native_notice_recovers_real_approval(identity_world: Any, finance_database: Any,
                                                          tmp_path: Any, monkeypatch: Any) -> None:
    """当前通知只定位原计划，标记已读也不能代替业务批准与本人执行。"""
    async with identity_world() as ident:
        platform,store=ident.world.platform,finance_database
        people,cookies,scope,step=await f2_people_and_expenses(ident,store)
        provider,id=await prepare_finance(store,people,scope,step,tmp_path)
        key,seen=Ed25519PrivateKey.generate(),[]
        config=await f2_signer_config(ident,key);monkeypatch.setattr(platform.runtimes,'get',f2_no_model)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(platform)),base_url='https://xuanpu.example.com') as online,httpx.AsyncClient(transport=httpx.MockTransport(tax_response)) as external:
            gateway=PlatformGateway(online,tenant_tokens={'acme':await ident.system_key(role='tenant_member')})
            platform.manual_connectors=ManualConnectorService(packages=platform.packages,vault=platform.vault,audit=platform.audit,signer=HumanContextSigner(config=config,vault=platform.vault),transport=VerifiedFinanceTransport(finance_app(store,gateway,key),key,seen))
            calls=F2CookieCalls(ident,cookies)
            registry=ComplianceRegistry([provider],external,tokens={('tax',1):'test'},callback_secrets={('tax',1):'s'*32})
            worker=SettlementWorker(ComplianceQueueAdapter(store),registry,gateway)
            await calls.call('owner','verifyInvoice',{'id':id,'Idempotency-Key':'verify','body':{'expected_version':1}},write=True);await worker.run_once()
            state=await calls.call('payer','getInvoiceCompliance',{'id':id})
            plan=await calls.action('payer',{'kind':'invoice','id':id,'version':state['version']},'confirm_usage',accounting_period='2026-10')
            await store.refresh_finance_notifications()
            notices=await calls.call('approver','listFinanceNotifications',{'legal_entity_id':'company-a'})
            notice=next(n for n in notices['items'] if n['target_id']==plan['id'])
            await calls.call('approver','readFinanceNotification',{'id':notice['id'],'body':{'expected_version':notice['version']},'Idempotency-Key':'read'},write=True)
            assert not (await calls.call('payer','getActionPlan',{'id':plan['id']}))['executable']
            queue=await calls.call('approver','listReviewQueue',{'legal_entity_id':'company-a'})
            review=await calls.action('approver',{'kind':'action_plan','id':plan['id'],'version':1},'approve',workflow_step_id=queue['items'][0]['workflow_step_id'])
            await calls.execute('approver',review)
            ready=await calls.call('payer','getActionPlan',{'id':plan['id']});await calls.execute('payer',ready);await worker.run_once()
            assert (await calls.call('payer','getInvoiceCompliance',{'id':id}))['snapshot']['usage_status']=='confirmed'
            overview=await calls.call('payer','getFinanceOverview',{'legal_entity_id':'company-a'})
            assert overview['runtime']['mode']=='enabled' and overview['notifications']['external_channel']=='none'
        assert seen and all(item['origin']=='human' for item in seen)
