"""F8以真实收件验证本人材料目录与未验真元数据。"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

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

RAW = b'%PDF-1.4 real supporting original'


class PdfGateway(Gateway):
    """显式提供实际PDF字节和原文件元数据。"""

    async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
        """保留真实网关计数并返回实际文件名。"""
        data, _, _ = await super().download(who, file)
        return data, 'application/pdf', file.file_id + '.pdf'


async def completed(store: FinanceStore, tmp_path: Path) -> tuple[dict[str, Any], PdfGateway]:
    """通过真实worker完成材料收件，禁止手工伪造成功数据。"""
    await prepare(store)
    op = await store.accept_supporting_materials(WHO, 'company-a', [ref('proof', RAW)], 'f8-proof')
    gateway = PdfGateway({'proof': RAW})
    assert await FinanceWorker(store, EncryptedLocalObjects(tmp_path, b'k' * 32), gateway).run_once()
    return op, gateway


async def test_pending_restarted_material_metadata_and_idempotency(store: FinanceStore, tmp_path: Path) -> None:
    """待收件不编造元数据，重建store只读真实回执，重试不增操作或回执。"""
    await prepare(store)
    files = [ref('proof', RAW)]
    op = await store.accept_supporting_materials(WHO, 'company-a', files, 'f8-proof')
    pending = await store.get_supporting_material_import(WHO, op['id'])
    assert pending['materials'] == [{'position': 0, 'state': 'pending', 'receipt_id': None,
        'filename': None, 'size_bytes': None, 'media_type': None, 'sha256': None,
        'verification_state': 'not_checked', 'selectable': False}]
    gateway = PdfGateway({'proof': RAW})
    assert await FinanceWorker(store, EncryptedLocalObjects(tmp_path, b'k' * 32), gateway).run_once()
    restored = FinanceStore(store.pool)
    detail = await restored.get_supporting_material_import(WHO, op['id'])
    assert detail['operation'] == await restored.get_operation(WHO, op['id'])
    material = detail['materials'][0]
    assert material == {'position': 0, 'state': 'material_received', 'receipt_id': material['receipt_id'],
        'filename': 'proof.pdf', 'size_bytes': len(RAW), 'media_type': 'application/pdf',
        'sha256': files[0].sha256, 'verification_state': 'not_checked', 'selectable': True}
    assert material['receipt_id'] and 'object_key' not in json.dumps(detail)
    listing = await restored.list_supporting_material_imports(WHO, 'company-a')
    assert listing['total'] == 1 and listing['next_cursor'] is None
    assert set(listing['items'][0]) == {'id', 'version', 'status', 'created_at', 'total', 'summary'}
    assert listing['items'][0]['summary'] == detail['operation']['summary']
    assert listing['items'][0]['total'] == 1 and 'file_id' not in json.dumps(listing)
    assert (await restored.accept_supporting_materials(WHO, 'company-a', files, 'f8-proof'))['id'] == op['id']
    assert not await FinanceWorker(restored, EncryptedLocalObjects(tmp_path, b'k' * 32), gateway).run_once()
    assert await store.pool.fetchval('SELECT count(*) FROM finance_operation') == 1
    assert await store.pool.fetchval('SELECT count(*) FROM finance_receipt') == 1
    assert await store.pool.fetchval('SELECT count(*) FROM finance_execution_grant') == 0


async def test_owner_purpose_tenant_and_current_grant_isolation(store: FinanceStore, tmp_path: Path) -> None:
    """所有其他岗位均不能越过owner，发票导入与撤权也不可借新入口读取。"""
    op, _ = await completed(store, tmp_path)
    for role in ['collector', 'reviewer', 'approver']:
        await store.grant(WHO.tenant_id, role, 'company-a', role)
        other = FinancePrincipal(WHO.tenant_id, role)
        assert (await store.list_supporting_material_imports(other, 'company-a'))['total'] == 0
        with pytest.raises(FinanceError, match='RESOURCE_NOT_FOUND'):
            await store.get_supporting_material_import(other, op['id'])
    await store.seed_company('tenant-b', 'company-a', 'B', 'B')
    await store.grant('tenant-b', WHO.user_id, 'company-a')
    other = FinancePrincipal('tenant-b', WHO.user_id)
    assert (await store.list_supporting_material_imports(other, 'company-a'))['total'] == 0
    with pytest.raises(FinanceError, match='RESOURCE_NOT_FOUND'):
        await store.get_supporting_material_import(other, op['id'])
    invoice = await store.accept_import(WHO, 'company-a', [ref('invoice', RAW)], 'invoice')
    for id in [invoice['id'], 'missing']:
        with pytest.raises(FinanceError, match='RESOURCE_NOT_FOUND'):
            await store.get_supporting_material_import(WHO, id)
    assert (await store.list_supporting_material_imports(WHO, 'company-a'))['total'] == 1
    await store.pool.execute('DELETE FROM finance_grant WHERE user_id=$1', WHO.user_id)
    for method, target in [(store.get_supporting_material_import, op['id']),
                           (store.list_supporting_material_imports, 'company-a')]:
        with pytest.raises(FinanceError, match='COMPANY_FORBIDDEN'):
            await method(WHO, target)


async def test_equal_timestamp_desc_pages_bind_person_company_and_direction(store: FinanceStore) -> None:
    """同时间倒序复合键无重复，游标不能跨人、公司、方向或格式复用。"""
    await prepare(store)
    await store.seed_company(WHO.tenant_id, 'company-b', 'B', 'B')
    await store.grant(WHO.tenant_id, WHO.user_id, 'company-b')
    operations = [await store.accept_supporting_materials(WHO, 'company-a', [ref(str(n), RAW)], str(n)) for n in range(5)]
    await store.accept_supporting_materials(WHO, 'company-b', [ref('b', RAW)], 'b')
    await store.pool.execute("UPDATE finance_operation SET created_at='2026-10-22T00:00:00Z'")
    first = await store.list_supporting_material_imports(WHO, 'company-a', limit=2)
    ids = [item['id'] for item in first['items']]
    cursor = first['next_cursor']
    while cursor:
        current = await store.list_supporting_material_imports(WHO, 'company-a', cursor, 2)
        assert current['total'] == 5
        ids.extend(item['id'] for item in current['items'])
        cursor = current['next_cursor']
    assert ids == sorted([op['id'] for op in operations], reverse=True)
    await store.grant(WHO.tenant_id, 'reviewer', 'company-a', 'reviewer')
    for who, entity in [(WHO, 'company-b'), (FinancePrincipal(WHO.tenant_id, 'reviewer'), 'company-a')]:
        with pytest.raises(FinanceError, match='CURSOR_INVALID'):
            await store.list_supporting_material_imports(who, entity, first['next_cursor'])
    for cursor in ['', 'bad', '中文']:
        with pytest.raises(FinanceError, match='CURSOR_INVALID'):
            await store.list_supporting_material_imports(WHO, 'company-a', cursor)
    for limit in [0, 101]:
        with pytest.raises(FinanceError, match='PAGINATION_INVALID'):
            await store.list_supporting_material_imports(WHO, 'company-a', limit=limit)


@pytest.mark.parametrize('tamper', ['receipt', 'position', 'hash', 'ref', 'object', 'size', 'type', 'owner', 'simulation'])
async def test_broken_responsibility_never_selectable(store: FinanceStore, tmp_path: Path, tamper: str) -> None:
    """收件状态不能替代回执与逐项、所有者和环境的完整责任链。"""
    op, _ = await completed(store, tmp_path)
    sql = {
        'receipt': "UPDATE finance_item SET receipt_id='missing'",
        'position': 'UPDATE finance_item SET position=1',
        'hash': "UPDATE finance_item SET actual_sha256=repeat('0',64)",
        'ref': "UPDATE finance_receipt SET file_ref=jsonb_set(file_ref,'{revision}','2')",
        'object': "UPDATE finance_item SET object_key='other-object'",
        'size': 'UPDATE finance_item SET size_bytes=0',
        'type': 'UPDATE finance_item SET content_type=NULL',
        'owner': "UPDATE finance_receipt SET owner_id='other'",
        'simulation': 'UPDATE finance_operation SET source_simulation=true',
    }
    if tamper == 'position':
        constraint = await store.pool.fetchval("SELECT conname FROM pg_constraint WHERE conrelid='finance_receipt'::regclass AND contype='f'")
        await store.pool.execute(f'ALTER TABLE finance_receipt DROP CONSTRAINT "{constraint}"')
    await store.pool.execute(sql[tamper])
    detail = await store.get_supporting_material_import(WHO, op['id'])
    assert detail['materials'][0]['selectable'] is False
    assert 'object_key' not in json.dumps(detail)


@pytest.mark.parametrize('cancel', [False, True])
async def test_partial_or_cancel_preserves_received_item_selection(store: FinanceStore, tmp_path: Path, cancel: bool) -> None:
    """实际部分成功或中途取消均保留已收件项可选，失败项不能选择。"""
    await prepare(store)

    class PausingGateway(PdfGateway):
        """第二项暂停，允许真实取消或真实下载错误。"""

        async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
            """首项实际完成后阻塞下一下载。"""
            if file.file_id == 'second':
                ready.set()
                await resume.wait()
            return await super().download(who, file)

    ready, resume = asyncio.Event(), asyncio.Event()
    op = await store.accept_supporting_materials(WHO, 'company-a', [ref('first', RAW), ref('second', RAW)], 'mixed')
    task = asyncio.create_task(FinanceWorker(store, EncryptedLocalObjects(tmp_path, b'k' * 32), PausingGateway({'first': RAW})).run_once())
    await asyncio.wait_for(ready.wait(), 10)
    if cancel:
        current = await store.get_operation(WHO, op['id'])
        await store.cancel_operation(WHO, op['id'], current['version'], 'cancel-mixed')
    resume.set()
    assert await task
    detail = await store.get_supporting_material_import(WHO, op['id'])
    assert detail['operation']['status'] == ('cancelled' if cancel else 'succeeded')
    assert detail['operation']['business_result'] == ('cancelled' if cancel else 'partial')
    assert [item['selectable'] for item in detail['materials']] == [True, False]
    assert detail['materials'][1]['state'] == ('cancelled' if cancel else 'failed')
