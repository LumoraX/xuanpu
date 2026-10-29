"""F7普通报销的原始粘贴、不可变历史及实际附件授权验收。"""
from __future__ import annotations

import base64
import importlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ExpenseDraft, ExpenseUpdate
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore

from .test_expense_domain import claim, execute, policy, preview, setup
from .test_f5_archive_formats import actual_pdf
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from pathlib import Path


def expense_body(**changes: Any) -> dict[str, Any]:
    """构造只有员工事实的普通报销。"""
    return {'legal_entity_id':'e','reason':'出差','lines':[{'category_id':'travel','amount':'10.00','occurred_on':'2026-10-22','reason':'地铁'}],**changes}


async def configured(store: FinanceStore, *, two: bool = True) -> tuple[Any, Any]:
    """启用显式内部记录制度，但不提供任何支付或查验权限。"""
    owner, people = await setup(store,two=two)
    p=policy(two=two,version=2).model_copy(update={'record_mode':'internal_record'})
    await store.configure_expenses([p])
    return owner,people


async def source(store: FinanceStore, owner: Any, objects: Any, name: str, *, invoice_import: bool = False, entity: str = 'e') -> tuple[str,bytes]:
    """真实PDF经原收件worker保全，保留未解析、未查验语义。"""
    raw=actual_pdf()+b'\n% '+name.encode()
    class PDFGateway(Gateway):
        """实际下载固定PDF字节和正确媒体类型。"""
        async def download(self, who: Any, file: Any) -> tuple[bytes,str,str]:
            """返回原件内容，不伪造结构化发票。"""
            return raw,'application/pdf',name+'.pdf'
    accept=store.accept_import if invoice_import else store.accept_supporting_materials
    op=await accept(owner,entity,[ref(name,raw)],name)
    assert await FinanceWorker(store,objects,PDFGateway({name:raw})).run_once()
    receipt=await store.pool.fetchval('SELECT id FROM finance_receipt WHERE operation_id=$1',op['id'])
    return receipt,raw


async def original_doc(store: FinanceStore,tmp_path: Path, *, invoice_import: bool = False) -> tuple[Any,Any,dict[str,Any],Any,bytes,str]:
    """配置真实公司、材料与仅内部记录草稿。"""
    owner,people=await configured(store)
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    receipt,raw=await source(store,owner,objects,'old',invoice_import=invoice_import)
    doc=await store.create_expense_claim(owner,ExpenseDraft(**expense_body(evidence_ids=[receipt])),'doc')
    return owner,people,doc,objects,raw,receipt


async def act(store: FinanceStore,who: Any,doc: dict[str,Any],action: str,**params: Any) -> dict[str,Any]:
    """实际人工预检并执行，再恢复服务端版本。"""
    plan=await preview(store,who,doc,action,**params)
    assert not plan['blockers'],plan['blockers']
    await execute(store,who,plan)
    return await store.get_expense_claim(who,doc['id'])


async def test_invoice_information_is_exact_optional_and_internal_only(store: FinanceStore) -> None:
    """粘贴文本原样持久化且不能制造发票、已验真或正式执行事实。"""
    owner,_=await configured(store)
    pasted='  发票号码：001\n金额 10.00\t备注  '
    doc=await store.create_expense_claim(owner,ExpenseDraft(**expense_body(invoice_information=pasted)),'pasted')
    assert doc['content']['invoice_information']==pasted
    assert doc['total']=='10.00' and doc['record_mode']=='internal_record'
    assert ExpenseDraft(**expense_body(invoice_information='x'*20000)).invoice_information=='x'*20000
    old=await claim(store,owner,key='legacy')
    assert old['content'].get('invoice_information') is None
    assert await store.pool.fetchval('SELECT count(*) FROM finance_invoice')==0
    assert await store.pool.fetchval('SELECT count(*) FROM finance_execution_grant')==0
    await store.configure_expenses([policy(version=3)])
    with pytest.raises(FinanceError):
        await store.create_expense_claim(owner,ExpenseDraft(**expense_body(invoice_information=pasted)),'backed')


@pytest.mark.parametrize('value',['',' \n\t','x'*20001,12,True,'号\x00码','\ud800','\udfff'])
def test_invoice_information_rejects_invalid_values(value: Any) -> None:
    """严格拒绝空白、过长及非文本，不对合法原文strip。"""
    with pytest.raises(ValidationError):
        ExpenseDraft(**expense_body(invoice_information=value))


def test_invoice_information_rejects_forged_verification() -> None:
    """额外核验状态继续被严格拒绝。"""
    with pytest.raises(ValidationError):
        ExpenseDraft(**expense_body(invoice_information='号码1',verified=True))


async def test_history_two_step_reject_revise_withdraw_pagination_and_restart(store: FinanceStore) -> None:
    """原快照和实际操作者跨重组保留，分页总数独立且游标不跨用户或单据。"""
    owner,people=await configured(store)
    doc=await claim(store,owner)
    doc=await act(store,owner,doc,'submit_claim')
    doc=await act(store,people[0],doc,'approve',workflow_step_id=doc['workflow']['current_step_id'],reason='主管同意')
    doc=await act(store,people[1],doc,'reject',workflow_step_id=doc['workflow']['current_step_id'],reason='缺行程')
    doc=await store.update_expense_claim(owner,doc['id'],ExpenseUpdate(expected_version=doc['version'],draft=ExpenseDraft(**expense_body(invoice_information=' 修订原文 '))),'revise')
    doc=await act(store,owner,doc,'submit_claim')
    doc=await act(store,owner,doc,'withdraw',reason='本人撤回')
    revived=FinanceStore(store.pool)
    assert hasattr(revived,'list_expense_claim_history'),'F7历史读取尚未实现'
    first=await revived.list_expense_claim_history(owner,doc['id'],limit=1)
    items=first['items'];cursor=first['next_cursor']
    while cursor:
        result=await revived.list_expense_claim_history(owner,doc['id'],cursor,1)
        assert result['total']==first['total']
        items+=result['items'];cursor=result['next_cursor']
    assert first['total']==len(items) and len({i['id'] for i in items})==len(items)
    decisions=[i['data'] for i in items if i['kind']=='decision']
    assert [(i['actor_id'],i['action']) for i in decisions]==[('a','approve'),('b','reject'),('owner','withdraw')]
    assert decisions[1]['parameters']['reason']=='缺行程'
    assert decisions[2]['step_id'] is None
    snapshots=await store.pool.fetch('SELECT version,data FROM finance_expense_history WHERE id=$1 ORDER BY version',doc['id'])
    assert [i['data'] for i in items if i['kind']=='revision']==[json.loads(r['data']) for r in snapshots]
    assert len(snapshots)==len({i['id'] for i in items if i['kind']=='revision'})
    for who,target in [(people[0],doc['id']),(owner,(await claim(store,owner,key='other'))['id']),(FinancePrincipal('other','owner','human'),doc['id'])]:
        with pytest.raises(FinanceError):
            await revived.list_expense_claim_history(who,target,first['next_cursor'],1)
    await store.grant('t','a','e','collector');await store.grant('t','a','e','approver')
    with pytest.raises(FinanceError):
        await revived.list_expense_claim_history(people[0],doc['id'])


@pytest.mark.parametrize('invoice_import',[False,True])
async def test_material_actual_pdf_history_and_current_assignment(store: FinanceStore,tmp_path: Path,invoice_import: bool) -> None:
    """当前授权人员可读指定历史版本的真实原件，恢复旧岗位代数不能重获权限。"""
    owner,people,doc,objects,raw,receipt=await original_doc(store,tmp_path,invoice_import=invoice_import)
    assert importlib.util.find_spec('xuanpu_finance.expenses.materials') is not None,'F7原件读取尚未实现'
    read=importlib.import_module('xuanpu_finance.expenses.materials').read_material
    for who,rid in [(people[0],receipt),(FinancePrincipal('t','owner','agent'),receipt),(owner,'guessed'),(FinancePrincipal('other','owner','human'),receipt)]:
        with pytest.raises(FinanceError):
            await read(store,objects,who,doc['id'],rid,0,128)
    await store.seed_company('t','other','他司','OTHER');await store.grant('t','stranger','other','reviewer')
    with pytest.raises(FinanceError):
        await read(store,objects,FinancePrincipal('t','stranger','human'),doc['id'],receipt,0,128)
    doc=await act(store,owner,doc,'submit_claim')
    result=await read(store,objects,people[0],doc['id'],receipt,0,128)
    assert base64.b64decode(result['content_base64'])==raw[:128]
    assert result['media_type']=='application/pdf' and 'object_key' not in result
    doc=await act(store,people[0],doc,'reject',workflow_step_id=doc['workflow']['current_step_id'],reason='补充材料')
    newer,newraw=await source(store,owner,objects,'new')
    doc=await store.update_expense_claim(owner,doc['id'],ExpenseUpdate(expected_version=doc['version'],draft=ExpenseDraft(**expense_body(evidence_ids=[newer]))),'replace')
    with pytest.raises(FinanceError):
        await read(store,objects,owner,doc['id'],receipt,0,128)
    chunks=[];offset=0
    while offset is not None:
        result=await read(store,objects,people[0],doc['id'],receipt,offset,128,document_version=1)
        chunks.append(base64.b64decode(result['content_base64']));offset=result['next_offset']
    assert b''.join(chunks)==raw and result['document_version']==1
    assert base64.b64decode((await read(store,objects,owner,doc['id'],newer,0,len(newraw)))['content_base64'])==newraw
    for version in [0,doc['version']+1]:
        with pytest.raises(FinanceError):
            await read(store,objects,owner,doc['id'],receipt,0,128,document_version=version)
    await store.grant('t','a','e','collector');await store.grant('t','a','e','approver')
    with pytest.raises(FinanceError):
        await read(store,objects,people[0],doc['id'],receipt,0,128,document_version=1)
    assert await store.pool.fetchval('SELECT count(*) FROM finance_invoice')==0
    if invoice_import:
        assert await store.pool.fetchval('SELECT state FROM finance_item WHERE operation_id=(SELECT operation_id FROM finance_receipt WHERE id=$1)',receipt)=='awaiting_original'


async def test_material_integrity_receipt_scope_and_unassigned_role(store: FinanceStore,tmp_path: Path) -> None:
    """不能猜同公司他人回执，完整原件与收件责任任一不符均阻断读取。"""
    owner,_,doc,objects,raw,receipt=await original_doc(store,tmp_path)
    read=importlib.import_module('xuanpu_finance.expenses.materials').read_material
    unused,_=await source(store,owner,objects,'unlinked')
    with pytest.raises(FinanceError):
        await read(store,objects,owner,doc['id'],unused,0,128)
    await store.seed_company('t','other-company','他司','OTHER')
    await store.grant('t','owner','other-company')
    other_receipt,_=await source(store,owner,objects,'other-company-source',entity='other-company')
    with pytest.raises(FinanceError):
        await read(store,objects,owner,doc['id'],other_receipt,0,128)
    await store.grant('t','unassigned','e','approver')
    with pytest.raises(FinanceError):
        await read(store,objects,FinancePrincipal('t','unassigned','human'),doc['id'],receipt,0,128)
    for offset,limit in [(-1,10),(len(raw),10),(0,0),(0,262145)]:
        with pytest.raises(FinanceError):
            await read(store,objects,owner,doc['id'],receipt,offset,limit)
    row=await store.pool.fetchrow('SELECT object_key,operation_id FROM finance_receipt WHERE id=$1',receipt)
    assert row is not None
    path=objects._path(row['object_key'])
    path.write_bytes(objects.envelope.encrypt(row['object_key'],raw+b'tamper'))
    with pytest.raises(FinanceError,match='MATERIAL_SOURCE_INVALID'):
        await read(store,objects,owner,doc['id'],receipt,0,128)
    path.write_bytes(objects.envelope.encrypt(row['object_key'],raw))
    await store.pool.execute("UPDATE finance_item SET object_key='other/key' WHERE operation_id=$1",row['operation_id'])
    with pytest.raises(FinanceError,match='MATERIAL_SOURCE_INVALID'):
        await read(store,objects,owner,doc['id'],receipt,0,128)


@pytest.mark.parametrize('kind',['funding_case','payable'])
async def test_expense_record_endpoints_refuse_other_document_kinds(store: FinanceStore,tmp_path: Path,kind: str) -> None:
    """共享底表的资金和应付真实单据不能绕过普通报销类型边界。"""
    if kind=='funding_case':
        from .test_f5_archive import real_funding_source
        owner,_,_,doc,objects,_=await real_funding_source(store,tmp_path)
        receipt=doc['content']['evidence_ids'][0]
    else:
        from .test_f2_domain import configured as payable_configured
        from .test_f2_domain import payable
        owner,_,_,_=await payable_configured(store)
        doc=await payable(store,owner)
        objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
        receipt='guessed'
    read=importlib.import_module('xuanpu_finance.expenses.materials').read_material
    with pytest.raises(FinanceError,match='RESOURCE_NOT_FOUND'):
        await read(store,objects,owner,doc['id'],receipt,0,128)
    with pytest.raises(FinanceError,match='RESOURCE_NOT_FOUND'):
        await store.list_expense_claim_history(owner,doc['id'])


async def test_actual_allocation_original_is_available_in_exact_saved_version(store: FinanceStore,tmp_path: Path) -> None:
    """真实解析发票分配产生的证据可读，不依靠人工伪造发票或快照。"""
    from .test_expense_domain import allocate
    from .test_parser import SAMPLE
    owner,_=await configured(store)
    objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    op=await store.accept_import(owner,'e',[ref('invoice',SAMPLE)],'invoice')
    assert await FinanceWorker(store,objects,Gateway({'invoice':SAMPLE}),synthetic=True).run_once()
    invoice_id=await store.pool.fetchval('SELECT id FROM finance_invoice')
    receipt=await store.pool.fetchval('SELECT id FROM finance_receipt WHERE operation_id=$1',op['id'])
    doc=await claim(store,owner,amount='10.00')
    await allocate(store,owner,doc,id=invoice_id)
    doc=await store.get_expense_claim(owner,doc['id'])
    read=importlib.import_module('xuanpu_finance.expenses.materials').read_material
    result=await read(store,objects,owner,doc['id'],receipt,0,len(SAMPLE),document_version=doc['version'])
    assert base64.b64decode(result['content_base64'])==SAMPLE
    with pytest.raises(FinanceError):
        await read(store,objects,owner,doc['id'],receipt,0,128,document_version=1)


async def test_two_actual_approvals_keep_intermediate_decision_without_invented_revision(store: FinanceStore) -> None:
    """两级人工审批中的第一步只有真实决定，不能从最终状态反推或伪造修订。"""
    owner,people=await configured(store)
    doc=await claim(store,owner)
    doc=await act(store,owner,doc,'submit_claim')
    submitted_version=doc['version']
    doc=await act(store,people[0],doc,'approve',workflow_step_id=doc['workflow']['current_step_id'])
    assert doc['version']==submitted_version and doc['status']=='under_review'
    doc=await act(store,people[1],doc,'approve',workflow_step_id=doc['workflow']['current_step_id'])
    assert doc['status']=='approved'
    result=await store.list_expense_claim_history(owner,doc['id'])
    decisions=[i['data'] for i in result['items'] if i['kind']=='decision']
    assert [(i['actor_id'],i['action']) for i in decisions]==[('a','approve'),('b','approve')]
    assert all(i['plan_id'] and i['workflow_id'] and i['step_id'] for i in decisions)
    assert len([i for i in result['items'] if i['kind']=='revision'])==3
