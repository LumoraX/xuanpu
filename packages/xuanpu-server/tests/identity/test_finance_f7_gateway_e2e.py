"""真实cookie/CSRF与无模型入口完成内部报销及独立原件历史核对。"""
from __future__ import annotations

import base64
import hashlib
import io
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pypdf import PdfWriter
from xuanpu_finance.expenses.models import ExpensePolicy
from xuanpu_finance.gateway import PlatformGateway
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore
from xuanpu_server.api import create_app
from xuanpu_server.manual_connectors import ManualConnectorService
from xuanpu_server.runtime.call_context import HumanContextSigner

from .test_finance_gateway_e2e import (
    F2CookieCalls,
    VerifiedFinanceTransport,
    f2_no_model,
    f2_people_and_expenses,
    f2_signer_config,
    finance_app,
)
from .test_finance_gateway_e2e import finance_database as _finance_database

if TYPE_CHECKING:
    from pathlib import Path

finance_database = _finance_database


async def record_people(ident: Any, store: FinanceStore) -> tuple[Any, Any]:
    """只配置内部费用制度与两级真人，不配置任何外部供应商。"""
    people, cookies, scope, step = await f2_people_and_expenses(ident, store)
    await store.configure_expenses([ExpensePolicy(
        **scope, version=2, enabled=True, record_mode='internal_record',
        categories=[{'id':'other','name':'其他费用'}], cost_centers=[],
        steps=[step, {'id':'finance','name':'财务复核','approver_user_id':people['accountant'].user_id}],
        exception_owner_user_id=people['accountant'].user_id, require_verified_invoice=False,
    )])
    return people, cookies


async def receive_pdf(ident: Any, calls: Any, store: Any, objects: Any, gateway: Any, cookies: Any) -> tuple[str, bytes]:
    """平台真实上传有效测试PDF，再独立加密保全并取得收件回执。"""
    writer, stream = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({'/Title':'内部报销测试附件，非真实发票'})
    writer.write(stream)
    raw = stream.getvalue()
    headers = {**cookies['owner'], 'X-Requested-By':'xuanpu',
               'Origin':str(ident.http.base_url).rstrip('/'), 'Content-Type':'application/pdf'}
    response = await ident.http.post('/v1/workspace/files?name=receipt.pdf&request_id=f7-receipt', headers=headers, content=raw)
    assert response.is_success, response.text
    file = response.json()
    ref = {'file_id':file['id'],'revision':file['data']['current_revision'],'sha256':hashlib.sha256(raw).hexdigest()}
    op = await calls.call('owner','importSupportingMaterials', {'body':{'legal_entity_id':'company-a','files':[ref]},'Idempotency-Key':'f7-receive'}, write=True)
    assert await FinanceWorker(store,objects,gateway).run_once()
    received = await calls.call('owner','getOperation',{'id':op['id']})
    assert received['purpose']=='supporting_material' and received['summary']['materials']==1
    return received['result_refs'][0]['id'], raw


async def review(calls: Any, doc: Any, actor: str, action: str, **parameters: Any) -> None:
    """当前队列步骤经真实人工预检和确认执行，保留意见。"""
    queue = await calls.call(actor,'listReviewQueue',{'legal_entity_id':'company-a'})
    step = next(item for item in queue['items'] if item['target_id']==doc['id'])
    plan = await calls.action(actor,doc,action,workflow_step_id=step['workflow_step_id'],**parameters)
    assert plan['executable'], plan['blockers']
    await calls.execute(actor,plan)


async def finish_record(calls: Any, doc: Any, receipt: str, raw: bytes) -> None:
    """驳回补充重提后逐级批准，原件字节与全部意见均保留。"""
    original = doc['content']['invoice_information']
    await calls.execute('owner',await calls.action('owner',doc,'submit_claim'))
    doc = await calls.call('owner','getExpenseClaim',{'id':doc['id']})
    material = await calls.call('approver','getExpenseMaterial',{'id':doc['id'],'receipt_id':receipt})
    assert base64.b64decode(material['content_base64']) == raw
    await review(calls,doc,'approver','reject',reason='请补充交通行程')
    doc = await calls.call('owner','getExpenseClaim',{'id':doc['id']})
    revised = {**doc['content'],'reason':'补充行程后重新报销'}
    doc = await calls.call('owner','updateExpenseClaim',{'id':doc['id'],'body':{'expected_version':doc['version'],'draft':revised},'Idempotency-Key':'f7-revise'},write=True)
    await calls.execute('owner',await calls.action('owner',doc,'submit_claim'))
    doc = await calls.call('owner','getExpenseClaim',{'id':doc['id']})
    await review(calls,doc,'approver','approve',reason='行程已补齐')
    await review(calls,doc,'accountant','approve',reason='内部记录已复核')
    done = await calls.call('owner','getExpenseClaim',{'id':doc['id']})
    assert done['status']=='approved' and done['paid_amount']=='0.00' and done['posted_amount']=='0.00'
    assert done['content']['invoice_information']==original
    items, cursor = [], None
    while True:
        page = await calls.call('owner','listExpenseClaimHistory',{'id':doc['id'],'limit':1,**({'cursor':cursor} if cursor else {})})
        assert page['total']>=8
        items.extend(page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    decisions = [item['data'] for item in items if item['kind']=='decision']
    assert [d['action'] for d in decisions]==['reject','approve','approve']
    assert {d['parameters']['reason'] for d in decisions}=={'请补充交通行程','行程已补齐','内部记录已复核'}
    assert len({item['id'] for item in items})==len(items)


async def test_cookie_internal_record_original_history_and_reassembly(identity_world: Any,finance_database: FinanceStore,tmp_path: Path,monkeypatch: Any) -> None:
    """真实员工cookie发起只记录闭环，平台模型不可用且外部提交为零。"""
    async with identity_world() as ident:
        platform, store = ident.world.platform, finance_database
        _, cookies = await record_people(ident,store)
        key, seen = Ed25519PrivateKey.generate(), []
        config = await f2_signer_config(ident,key)
        monkeypatch.setattr(platform.runtimes,'get',f2_no_model)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(platform)),base_url='https://xuanpu.example.com') as online:
            gateway = PlatformGateway(online,tenant_tokens={'acme':await ident.system_key(role='tenant_member')})
            objects = EncryptedLocalObjects(tmp_path/'independent-originals',b'k'*32)
            finance = finance_app(store,gateway,key)
            finance.state.objects = objects
            platform.manual_connectors = ManualConnectorService(packages=platform.packages,vault=platform.vault,audit=platform.audit,signer=HumanContextSigner(config=config,vault=platform.vault),transport=VerifiedFinanceTransport(finance,key,seen))
            calls = F2CookieCalls(ident,cookies)
            receipt, raw = await receive_pdf(ident,calls,store,objects,gateway,cookies)
            body = {'legal_entity_id':'company-a','reason':'交通报销','invoice_information':'  发票号：TEST-ONLY\n金额100.00；仅申报，未查验  ',
                    'lines':[{'category_id':'other','amount':'100.00','occurred_on':'2026-10-22','reason':'交通'}],'evidence_ids':[receipt]}
            doc = await calls.call('owner','createExpenseClaim',{'body':body,'Idempotency-Key':'f7-draft'},write=True)
            await finish_record(calls,doc,receipt,raw)
            rebuilt = finance_app(FinanceStore(store.pool),gateway,key)
            rebuilt.state.objects = objects
            platform.manual_connectors.transport = VerifiedFinanceTransport(rebuilt,key,seen)
            history = await calls.call('owner','listExpenseClaimHistory',{'id':doc['id']})
            assert history['total']>=8
            assert await store.pool.fetchval('SELECT count(*) FROM finance_invoice')==0
            assert await store.pool.fetchval('SELECT count(*) FROM finance_settlement_operation')==0
            assert await store.pool.fetchval('SELECT count(*) FROM finance_compliance_operation')==0
        assert seen and all(claim['origin']=='human' for claim in seen)
