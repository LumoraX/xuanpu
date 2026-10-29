"""专用资金真实封存、补包及独立数据库/原件恢复。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import asyncpg
import httpx
from xuanpu_finance.compliance.archive import ArchivePipeline
from xuanpu_finance.compliance.evidence import EvidenceFetcher
from xuanpu_finance.compliance.models import ComplianceActionPreview
from xuanpu_finance.compliance.provider import ComplianceRegistry
from xuanpu_finance.compliance.queue import ComplianceQueueAdapter
from xuanpu_finance.maintenance import MaintenanceOptions, backup, restore
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.settlement.worker import SettlementWorker

from .f3_http_service import ComplianceService
from .test_expense_domain import execute
from .test_f3_guards import approve as approve_archive
from .test_f3_http_e2e import configure_http
from .test_f4_maintenance import maintenance_database as _maintenance_database
from .test_f4_maintenance import pg_tools
from .test_f5_archive import real_funding_source
from .test_f5_domain import approve, submit
from .test_worker import Gateway

if TYPE_CHECKING:
    from pathlib import Path

maintenance_database=_maintenance_database


async def archive_once(store: Any,tax: Any,audit: Any,doc: Any,worker: Any,*,parent: Any=None) -> dict[str,Any]:
    """真实专用审批后上传同一固定档案，必要时关联原清册和当前来源事件。"""
    parameters={} if parent is None else {'parent_manifest_hash':parent['manifest_hash'],'source_event_ids':parent['supplement_event_ids']}
    plan=await store.preview_finance_action(tax,ComplianceActionPreview(action='archive',target_type='funding_case',target_id=doc['id'],expected_version=doc['version'],parameters=parameters),'archive-'+uuid4().hex)
    assert len(plan['blockers'])==1,plan['blockers']
    plan=await store.get_action_plan(tax,plan['id'])
    assert 'workflow' in plan,json.dumps(plan,ensure_ascii=False)
    await approve_archive(store,audit,plan)
    plan=await store.get_action_plan(tax,plan['id']);assert plan['executable'],plan['blockers']
    op=await execute(store,tax,plan)
    assert await worker.run_once()
    current=await store.get_operation(tax,op['id'])
    assert current['status']=='succeeded',json.dumps(current,ensure_ascii=False)
    return (await store.list_archives(tax,'e'))['items'][0]


async def paid_parent(store: Any,owner: Any,approver: Any,doc: Any,origin: str,raw: bytes) -> Any:
    """通过F2真实派发责任接收隔离付款事实；实际TCP付款另有三场景回归。"""
    from xuanpu_finance.models import FinancePrincipal

    from .test_f2_domain import provider_result, ref
    from .test_f5_http_e2e import enable_provider, external_plan
    payer=FinancePrincipal('t','payer','human')
    config=await enable_provider(store,origin=origin)
    plan=await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref('supplier_account','account'))
    await execute(store,payer,plan)
    job=await store.claim_settlement();request=await store.prepare_settlement_dispatch(job)
    result=provider_result(request)
    result['evidence']=[{'id':'actual-file','sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw),'media_type':'application/json','object_ref':'isolated-proof'}]
    await store.record_settlement_result(job,result,json.dumps(result).encode());await store.release_settlement(job)
    return config


async def actual_return(store: Any,owner: Any,approver: Any,parent: Any,objects: Any) -> dict[str,Any]:
    """真实退款材料生成不可变内部责任与最后人工决定，供归档家庭和恢复验证。"""
    from xuanpu_finance.funding.models import FundingDraft
    from xuanpu_finance.invoices.worker import FinanceWorker

    from .test_worker import ref
    raw=b'<InternalRefund transaction="return-origin" amount="100.00"/>'
    op=await store.accept_supporting_materials(owner,'e',[ref('refund',raw)],'refund-source')
    await FinanceWorker(store,objects,Gateway({'refund':raw})).run_once()
    receipt=(await store.get_operation(owner,op['id']))['result_refs'][0]['id']
    body=FundingDraft(**{**parent['content'],'scenario':'prepayment_return','amount':'100.00','source_case_id':parent['id'],'evidence_ids':[receipt],'authority':{'kind':'return_evidence','reference':'return-origin','amount_limit':'100.00','occurred_on':'2026-10-22','evidence_ids':[receipt]}})
    child=await store.create_funding_case(owner,body,'return')
    return await approve(store,owner,approver,await submit(store,owner,child))


async def funding_archive_roundtrip(store: Any,tmp_path: Path) -> tuple[EncryptedLocalObjects,dict[str,bytes]]:
    """真实TCP归档读回包含材料及内部决定，新增子单使原包进入补包提醒。"""
    owner,tax,_,doc,objects,raw=await real_funding_source(store,tmp_path)
    from .test_f3_domain import configured
    _,_,audit=await configured(store)
    # 上述配置只恢复原F3当前指针，不替代F5的实际审批决定。
    from xuanpu_finance.models import FinancePrincipal

    from .test_f5_domain import policy
    await store.configure_funding([policy()])
    approver=FinancePrincipal('t','a','human')
    doc=await approve(store,owner,approver,await submit(store,owner,doc))
    service=ComplianceService();server=await asyncio.start_server(service.handle,'127.0.0.1',0)
    origin='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
    configs=await configure_http(store,tmp_path,origin)
    settlement_config=await paid_parent(store,owner,approver,doc,origin,service.file)
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=5) as http:
            registry=ComplianceRegistry(configs,http,tokens={(c.provider,2):'test' for c in configs},callback_secrets={(c.provider,2):'s'*32 for c in configs})
            from xuanpu_finance.settlement.providers import ProviderRegistry
            settlement=ProviderRegistry([settlement_config],http,tokens={('test',2):'test'},callback_secrets={('test',2):'s'*32})
            pipeline=ArchivePipeline(registry,objects,EvidenceFetcher({'compliance':registry,'settlement':settlement}));registry.archive=pipeline
            worker=SettlementWorker(ComplianceQueueAdapter(store,pipeline),registry,Gateway({}))
            archived=await archive_once(store,tax,audit,doc,worker)
            assert not archived['supplement_required']
            child=await actual_return(store,owner,approver,doc,objects)
            assert child['funding']['internal_attestation'] is not None
            # 封存后新增子单和授权撤销均是实际新事实，原家庭需另建补包。
            auth=doc['funding']['authority'];await store.revoke_funding_authority(tax,auth['id'],auth['version'],'材料事后需重新核对','revoke')
            changed=await store.get_archive(tax,archived['id']);assert changed['supplement_required']
            await store.refresh_finance_notifications()
            assert any(n['target_id']==archived['id'] for n in (await store.list_finance_notifications(tax,'e'))['items'])
            doc=await store.get_funding_case(owner,doc['id'])
            await archive_once(store,tax,audit,doc,worker,parent=changed)
            assert not (await store.get_archive(tax,archived['id']))['supplement_required']
            assert await store.pool.fetchval("SELECT count(*) FROM finance_funding_reservation WHERE state='completed'")==1
            from xuanpu_finance.maintenance_objects import archive_index
            indexed={}
            async with store.pool.acquire() as conn:
                await archive_index(conn,objects,indexed)
            all_bytes={key:await objects.get(key) for key in indexed}
            bundles=[value for key,value in all_bytes.items() if key.endswith('/bundle.zip')]
            assert len(bundles)==2
            for bundle in bundles:
                with zipfile.ZipFile(io.BytesIO(bundle)) as zip:
                    assert zip.testzip() is None
                    material_files=[p for p in zip.namelist() if p.startswith('funding/materials/')]
                    assert material_files and any(zip.read(path)==raw for path in material_files)
                    assert any('funding_authorities' in p for p in zip.namelist())
            expanded=[zipfile.ZipFile(io.BytesIO(bundle)) for bundle in bundles]
            try:
                assert any(child['id'].encode() in z.read(name) for z in expanded for name in z.namelist() if 'funding_cases' in name)
            finally:
                for z in expanded:
                    z.close()
            return objects,all_bytes
    finally:
        server.close();await server.wait_closed()


async def test_funding_actual_archive_and_independent_restore(maintenance_database: Any,tmp_path: Path) -> None:
    """真实新表、授权撤销决定、资金原材料和两个ZIP恢复后同摘要且不发起执行。"""
    store,options,dsns=maintenance_database
    objects,all_bytes=await funding_archive_roundtrip(store,tmp_path)
    before={table:await store.pool.fetch('SELECT * FROM '+table+' ORDER BY 1,2,3') for table in ['finance_funding_case','finance_funding_authority','finance_funding_original','finance_funding_authority_decision','finance_funding_reservation','finance_funding_policy_revision']}
    dump,load=pg_tools(tmp_path);folder=tmp_path/'backup'
    await backup(MaintenanceOptions(options[0].database_dsn_file,objects.root,options[0].key_file),folder,pg_dump=str(dump))
    report=await restore(options[1],folder,pg_restore=str(load))
    assert not report['workers_started'] and report['external_side_effects']==0
    conn=await asyncpg.connect(dsns[1])
    try:
        for table,rows in before.items():
            assert await conn.fetch('SELECT * FROM '+table+' ORDER BY 1,2,3')==rows
        target=EncryptedLocalObjects(options[1].object_root,b'k'*32)
        for key,raw in all_bytes.items():
            assert await target.get(key)==raw
    finally:
        await conn.close()
