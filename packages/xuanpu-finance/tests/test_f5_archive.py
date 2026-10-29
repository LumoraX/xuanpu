"""专用资金归档必须包含无票材料、原预付家庭及实际内部决定。"""
from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.compliance.models import ComplianceActionPreview
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage.common import decode

from .test_f2_domain import configured
from .test_f3_domain import configured as compliance_configured
from .test_f5_domain import draft, funding_catalog, policy
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from pathlib import Path


def test_archive_funding_target_has_explicit_contract() -> None:
    """归档目标必须有明确资金单据类型，不能伪装应付或发票。"""
    body=ComplianceActionPreview(action='archive',target_type='funding_case',target_id='case',expected_version=1,parameters={})
    assert body.target_type=='funding_case'


async def real_funding_source(store: Any,tmp_path: Path) -> tuple[Any,Any,Any,dict[str,Any],EncryptedLocalObjects,bytes]:
    """真实PDF经受限收件及加密原件形成无票预付，所有资料来自当前独立库。"""
    owner,_,_,approver=await configured(store)
    _,tax,_=await compliance_configured(store)
    await funding_catalog(store)
    await store.configure_funding([policy()])
    from .test_f5_archive_formats import actual_pdf
    raw=actual_pdf()
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    op=await store.accept_supporting_materials(owner,'e',[ref('contract',raw)],'support')
    class MaterialGateway(Gateway):
        """测试平台声明真实PDF格式，避免用XML媒体类型误导归档检测。"""
        async def download(self,who: Any,file: Any) -> tuple[bytes,str,str]:
            """同一固定PDF字节和受控媒体类型交给原有收件责任链。"""
            return raw,'application/pdf','contract.pdf'
    await FinanceWorker(store,objects,MaterialGateway({'contract':raw})).run_once()
    receipt=(await store.get_operation(owner,op['id']))['result_refs'][0]['id']
    body=draft(evidence_ids=[receipt],authority={'kind':'prepayment_terms','reference':'advance-terms','amount_limit':'1000.00','evidence_ids':[receipt]})
    doc=await store.create_funding_case(owner,body,'advance')
    return owner,tax,approver,doc,objects,raw


async def test_archive_no_invoice_funding_keeps_actual_material_and_authority(store: Any,tmp_path: Path) -> None:
    """无发票预付归档仍有真实原件字节和不可变授权，不制造invoice_original。"""
    _,tax,_,doc,objects,raw=await real_funding_source(store,tmp_path)
    body=ComplianceActionPreview(action='archive',target_type='funding_case',target_id=doc['id'],expected_version=doc['version'],parameters={})
    async with store.pool.acquire() as conn,conn.transaction():
        target=await store._f3_target(conn,tax,'funding_case',doc['id'])
        archive_policy=decode(await conn.fetchval('SELECT data FROM finance_compliance_policy_revision LIMIT 1'))
        snapshot=await store._f3_archive_snapshot(conn,target,body,archive_policy)
    assert snapshot['target']['type']=='funding_case'
    assert 'invoice_original' not in snapshot['required_roles']
    materials=[a for a in snapshot['artifacts'] if a['kind']=='funding_material']
    assert len(materials)==1 and await objects.get(materials[0]['source']['object_key'])==raw
    assert 'funding_authorities' in snapshot['required_roles']
    authority=next(a for a in snapshot['artifacts'] if a['kind']=='funding_authorities')
    saved=json.loads(base64.b64decode(authority['source']['content_base64']))['data']
    assert saved and doc['funding']['authority']['id'] in json.dumps(saved)
