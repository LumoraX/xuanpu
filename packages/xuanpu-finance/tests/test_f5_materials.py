"""支持材料复用真实收件责任索引，保持与发票解析和核实事实分离。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.models import FileRef, FinancePrincipal
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore

from .test_storage import WHO, prepare
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from pathlib import Path


async def test_supporting_pdf_and_image_preserved_without_fake_invoice(store: FinanceStore,tmp_path: Path) -> None:
    """真实PDF与图片独立保全，不建发票、不制造补票异常，也不声称材料已核实。"""
    await prepare(store)
    pdf,png=b'%PDF-1.4 contract source',b'\x89PNG\r\n\x1a\npayment proof'
    files=[ref('contract',pdf),ref('proof',png)]
    op=await store.accept_supporting_materials(WHO,'company-a',files,'materials')
    assert op['summary']['pending']==2
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    gateway=Gateway({'contract':pdf,'proof':png})
    assert await FinanceWorker(store,objects,gateway).run_once()
    result=await store.get_operation(WHO,op['id'])
    assert result['status']=='succeeded' and result['business_result']=='succeeded'
    assert result['summary']['materials']==2 and result['summary']['parsed']==0
    assert {item['state'] for item in result['items']}=={'material_received'}
    assert len(result['result_refs'])==2
    assert all(e['verification_state']=='not_checked' for e in result['result_refs'])
    assert (await store.list_invoices(WHO,'company-a'))['total']==0
    assert (await store.list_exceptions(WHO,'company-a'))['total']==0
    for row in await store.pool.fetch('SELECT object_key,file_ref FROM finance_receipt ORDER BY position'):
        assert await objects.get(row['object_key']) in {pdf,png}
    replay=await store.accept_supporting_materials(WHO,'company-a',files,'materials')
    assert replay['id']==op['id'] and gateway.downloads==2
    assert not await FinanceWorker(FinanceStore(store.pool),objects,gateway).run_once()


async def test_import_purpose_cannot_change_under_same_key(store: FinanceStore) -> None:
    """相同key不能把发票解析请求换成材料收件或相反，异内容也拒绝。"""
    await prepare(store)
    raw=b'%PDF-1.4 original'
    files=[ref('a',raw)]
    await store.accept_import(WHO,'company-a',files,'same')
    with pytest.raises(FinanceError,match='IDEMPOTENCY_CONFLICT'):
        await store.accept_supporting_materials(WHO,'company-a',files,'same')
    await store.accept_supporting_materials(WHO,'company-a',files,'material')
    with pytest.raises(FinanceError,match='IDEMPOTENCY_CONFLICT'):
        await store.accept_import(WHO,'company-a',files,'material')
    with pytest.raises(FinanceError,match='IDEMPOTENCY_CONFLICT'):
        await store.accept_supporting_materials(WHO,'company-a',[ref('a',b'other')],'material')


async def test_material_hash_mismatch_never_becomes_accepted_evidence(store: FinanceStore,tmp_path: Path) -> None:
    """持久材料来源摘要错误须隔离，不能凭收到bytes跳过与固定版本的核对。"""
    await prepare(store)
    op=await store.accept_supporting_materials(WHO,'company-a',[FileRef(file_id='bad',revision=1,sha256='0'*64)],'bad')
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    await FinanceWorker(store,objects,Gateway({'bad':b'%PDF-1.4 wrong'})).run_once()
    result=await store.get_operation(WHO,op['id'])
    assert result['status']=='failed' and result['summary']['materials']==0
    assert result['summary']['quarantined']==1
    assert result['items'][0]['error_code']=='FILE_HASH_MISMATCH'


async def test_material_recovers_original_after_source_disappears(store: FinanceStore,tmp_path: Path) -> None:
    """对象已写而回执未落库时，重启凭责任索引恢复，不重新依赖平台原件。"""
    await prepare(store)
    raw=b'%PDF-1.4 retained source'
    op=await store.accept_supporting_materials(WHO,'company-a',[ref('source',raw)],'retained')
    claim=await store.claim_import()
    item=await store.prepare_original(claim,0,ref('source',raw).sha256,len(raw),'application/pdf','source.pdf')
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    await objects.put(item['object_key'],raw)
    await store.release_import(claim)
    gateway=Gateway({})
    assert await FinanceWorker(FinanceStore(store.pool),objects,gateway).run_once()
    result=await store.get_operation(WHO,op['id'])
    assert result['summary']['materials']==1 and gateway.downloads==0
    assert await objects.get(item['object_key'])==raw


async def test_material_operation_does_not_cross_owner_or_company(store: FinanceStore) -> None:
    """支持材料与F0使用相同的真实租户、公司和员工可见边界。"""
    await prepare(store)
    op=await store.accept_supporting_materials(WHO,'company-a',[ref('source',b'original')],'access')
    await store.grant(WHO.tenant_id,'other','company-a','collector')
    for who in [FinancePrincipal(WHO.tenant_id,'other'),FinancePrincipal('t2',WHO.user_id)]:
        with pytest.raises(FinanceError):
            await store.get_operation(who,op['id'])
    with pytest.raises(FinanceError):
        await store.accept_supporting_materials(WHO,'missing-company',[ref('source',b'original')],'missing')


async def test_material_permission_revoked_during_object_read_cannot_commit(store: FinanceStore,tmp_path: Path) -> None:
    """实际解密后撤权只使当前项失败，保留既有回执和密文且无材料成功事件。"""
    await prepare(store)
    raw=b'%PDF-1.4 permission race'
    op=await store.accept_supporting_materials(WHO,'company-a',[ref('source',raw)],'revoke-race')

    class RevokingObjects(EncryptedLocalObjects):
        """在实际解密读取完成时触发同公司授权撤销。"""

        async def get(self, object_key: str) -> bytes:
            """复现外部等待与最终状态提交之间的撤权窗口。"""
            data=await super().get(object_key)
            await store.pool.execute('DELETE FROM finance_grant WHERE tenant_id=$1 AND user_id=$2',WHO.tenant_id,WHO.user_id)
            return data

    objects=RevokingObjects(tmp_path/'objects',b'k'*32)
    worker=FinanceWorker(store,objects,Gateway({'source':raw}))
    assert await worker.run_once()
    operation=await store.pool.fetchrow('SELECT status,business_result FROM finance_operation WHERE id=$1',op['id'])
    assert operation is not None and operation['status']=='failed' and operation['business_result']=='failed'
    item=await store.pool.fetchrow('SELECT state,error_code,receipt_id,object_key FROM finance_item WHERE operation_id=$1',op['id'])
    assert item is not None and item['state']=='failed' and item['error_code']=='COMPANY_FORBIDDEN'
    assert await store.pool.fetchval("SELECT count(*) FROM finance_event WHERE aggregate_id=$1 AND kind='material.received'",op['id'])==0
    assert await store.pool.fetchval('SELECT count(*) FROM finance_receipt WHERE operation_id=$1',op['id'])==1
    receipt=await store.pool.fetchrow('SELECT object_key FROM finance_receipt WHERE id=$1',item['receipt_id'])
    assert receipt is not None and receipt['object_key']==item['object_key']
    assert await objects.get(item['object_key'])==raw
    ciphers=list(objects.root.rglob('*.enc'))
    assert len(ciphers)==1 and raw not in ciphers[0].read_bytes()
    assert not await worker.run_once()
