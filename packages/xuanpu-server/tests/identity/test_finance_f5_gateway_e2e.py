"""真实员工cookie、平台文件及无模型人工审批连接独立专用资金库。"""
from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.funding.models import SCENARIOS, FundingPolicy
from xuanpu_finance.gateway import PlatformGateway
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.settlement.models import BusinessSnapshot
from xuanpu_server.api import create_app
from xuanpu_server.manual_connectors import ManualConnectorService
from xuanpu_server.runtime.call_context import HumanContextSigner

from .test_finance_gateway_e2e import (
    F2CookieCalls,
    VerifiedFinanceTransport,
    f2_no_model,
    f2_people_and_expenses,
    f2_ref,
    f2_settlement_policy,
    f2_settlements,
    f2_signer_config,
    finance_app,
)
from .test_finance_gateway_e2e import finance_database as _finance_database

if TYPE_CHECKING:
    from pathlib import Path

finance_database=_finance_database


async def funding_configuration(ident: Any,store: Any,tmp_path: Path) -> tuple[Any,Any]:
    """当前公司显式配置原供应商主体和合同，不从用户文字制造已核实事实。"""
    people,cookies,scope,step=await f2_people_and_expenses(ident,store)
    provider=await f2_settlements(store,people,scope,step,tmp_path)
    documents=[BusinessSnapshot(**scope,ref={**f2_ref(kind,kind),'version':'v2'},name=kind,
        verification_state='verified',allowed_user_ids=[people['owner'].user_id],
        party_tax_id='SELLER' if kind=='supplier' else 'BUYER',
        counterparty_tax_id=None if kind=='supplier' else 'SELLER') for kind in ['supplier','contract']]
    from xuanpu_finance.settlement.models import PayeeAccount
    from xuanpu_finance.storage.common import decode
    row=await store.pool.fetchval('SELECT data FROM finance_settlement_catalog_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND kind=$3','acme','company-a','supplier_account')
    await store.configure_settlements([f2_settlement_policy(scope,people,step)],documents,[PayeeAccount.model_validate(decode(row))],[provider.public_descriptor()],simulation=True)
    await store.configure_funding([FundingPolicy(**scope,version=1,enabled=True,steps={s:[step] for s in SCENARIOS})])
    return people,cookies


async def test_cookie_material_and_funding_hitl_with_model_off(identity_world: Any,finance_database: Any,tmp_path: Path,monkeypatch: Any) -> None:
    """真实平台上传→独立加密收件→本人提交→分派人批准，未调用模型或支付。"""
    async with identity_world() as ident:
        platform,store=ident.world.platform,finance_database
        people,cookies=await funding_configuration(ident,store,tmp_path)
        key,seen=Ed25519PrivateKey.generate(),[]
        config=await f2_signer_config(ident,key);monkeypatch.setattr(platform.runtimes,'get',f2_no_model)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(platform)),base_url='https://xuanpu.example.com') as online:
            gateway=PlatformGateway(online,tenant_tokens={'acme':await ident.system_key(role='tenant_member')})
            objects=EncryptedLocalObjects(tmp_path/'independent-originals',b'k'*32)
            finance=finance_app(store,gateway,key);finance.state.objects=objects
            platform.manual_connectors=ManualConnectorService(packages=platform.packages,vault=platform.vault,audit=platform.audit,signer=HumanContextSigner(config=config,vault=platform.vault),transport=VerifiedFinanceTransport(finance,key,seen))
            calls=F2CookieCalls(ident,cookies);raw=b'%PDF-1.4 actual employee contract'
            headers={**cookies['owner'],'X-Requested-By':'xuanpu','Origin':str(ident.http.base_url).rstrip('/'),'Content-Type':'application/pdf'}
            upload=await ident.http.post('/v1/workspace/files?name=contract.pdf&request_id=f5-contract',headers=headers,content=raw)
            assert upload.is_success,upload.text
            file=upload.json();file_ref={'file_id':file['id'],'revision':file['data']['current_revision'],'sha256':hashlib.sha256(raw).hexdigest()}
            op=await calls.call('owner','importSupportingMaterials',{'body':{'legal_entity_id':'company-a','files':[file_ref]},'Idempotency-Key':'support'},write=True)
            assert await FinanceWorker(store,objects,gateway).run_once()
            received=await calls.call('owner','getOperation',{'id':op['id']})
            assert received['summary']['materials']==1 and received['purpose']=='supporting_material'
            receipt=received['result_refs'][0]['id']
            body={'legal_entity_id':'company-a','scenario':'prepayment','reason':'预付采购款','amount':'100.00','due_date':'2026-11-01','supplier_ref':{**f2_ref('supplier','supplier'),'version':'v2'},'business_refs':[{**f2_ref('contract','contract'),'version':'v2'}],'evidence_ids':[receipt],'authority':{'kind':'prepayment_terms','reference':'terms','amount_limit':'100.00','evidence_ids':[receipt]}}
            doc=await calls.call('owner','createFundingCase',{'body':body,'Idempotency-Key':'draft'},write=True)
            plan=await calls.action('owner',doc,'submit_funding_case');await calls.execute('owner',plan)
            doc=await calls.call('owner','getFundingCase',{'id':doc['id']})
            queue=await calls.call('approver','listReviewQueue',{'legal_entity_id':'company-a'})
            review=await calls.action('approver',doc,'approve',workflow_step_id=queue['items'][0]['workflow_step_id']);await calls.execute('approver',review)
            done=await calls.call('owner','getFundingCase',{'id':doc['id']})
            assert done['status']=='approved' and done['paid_amount']=='0.00'
            assert done['funding']['internal_attestation']['actor_id']==people['approver'].user_id
            material=await calls.call('approver','getFundingMaterial',{'id':doc['id'],'receipt_id':receipt})
            assert base64.b64decode(material['content_base64'])==raw and material['next_offset'] is None
            saved=await store.pool.fetchrow('SELECT object_key,sha256 FROM finance_receipt WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3','acme','company-a',receipt)
            assert await objects.get(saved['object_key'])==raw
        assert seen and all(claim['origin']=='human' for claim in seen)
        assert await store.pool.fetchval('SELECT count(*) FROM finance_settlement_operation')==0
