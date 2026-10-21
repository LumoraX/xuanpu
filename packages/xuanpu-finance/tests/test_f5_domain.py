"""F5专用资金在真实独立PG上的严格契约和人工审批。"""
from __future__ import annotations

import importlib
import json
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionPreview
from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy
from xuanpu_finance.storage.common import decode

from .test_expense_domain import execute
from .test_f2_domain import configured, ref

SCENARIOS = ('prepayment', 'employee_advance', 'entrusted_collection',
             'prepayment_clearance', 'prepayment_return')


def models() -> Any:
    """缺失领域实现以清晰断言报告RED。"""
    assert importlib.util.find_spec('xuanpu_finance.funding') is not None, '缺少F5 funding包'
    return importlib.import_module('xuanpu_finance.funding.models')


def policy(**changes: Any) -> Any:
    """每一场景显式配置同一真实审批岗位。"""
    return models().FundingPolicy(**{
        'tenant_id': 't', 'legal_entity_id': 'e', 'version': 1, 'enabled': True,
        'steps': {s: [{'id': 'a', 'name': '资金业务核对', 'approver_user_id': 'a'}]
                  for s in SCENARIOS}, **changes})


async def material(store: Any, id: str = 'material', sha: str = 'b' * 64) -> None:
    """保存与F0实际责任链等价的已保全支持材料快照。"""
    file_ref = {'file_id': id, 'revision': 1, 'sha256': sha}
    await store.pool.execute(
        "INSERT INTO finance_operation(tenant_id,legal_entity_id,id,owner_id,idempotency_key,request_hash,status,business_result,trace_id,source_simulation,purpose) VALUES('t','e',$1,'owner',$1,'h','succeeded','succeeded',$1,true,'supporting_material')", id)
    await store.pool.execute(
        "INSERT INTO finance_item(tenant_id,legal_entity_id,operation_id,position,file_ref,state,receipt_id,object_key,actual_sha256,content_type,size_bytes) VALUES('t','e',$1,0,$2::jsonb,'material_received',$1,$1,$3,'application/pdf',12)", id, json.dumps(file_ref), sha)
    await store.pool.execute(
        "INSERT INTO finance_receipt VALUES('t','e',$1,'owner',$1,0,$1,$2,$3::jsonb)", id, sha, json.dumps(file_ref))


async def setup_funding(store: Any) -> tuple[Any, Any, Any, Any]:
    """沿用F2组织和独立专用经办，并显式启用F5。"""
    people = await configured(store)
    await funding_catalog(store)
    assert hasattr(store, 'configure_funding'), '缺少configure_funding领域配置'
    await store.configure_funding([policy()])
    await material(store)
    return people


def draft(scenario: str = 'prepayment', amount: str = '600.00', **changes: Any) -> Any:
    """所有事实均为严格声明，原件必须在独立库可核对。"""
    kinds = {'prepayment': 'prepayment_terms', 'employee_advance': 'employee_payment',
             'entrusted_collection': 'collection_mandate', 'prepayment_return': 'return_evidence'}
    authority: dict[str, Any] | None = None
    if scenario in kinds:
        authority = {'kind': kinds[scenario], 'reference': 'original-transaction',
                     'amount_limit': '1000.00', 'evidence_ids': ['material']}
        if scenario == 'entrusted_collection':
            authority.update(valid_from='2026-01-01', valid_until='2027-01-01',
                             account_ref=ref('supplier_account', 'account'))
        if scenario in {'employee_advance', 'prepayment_return'}:
            authority['occurred_on'] = '2026-10-21'
        if scenario == 'employee_advance':
            authority['account_ref'] = ref('employee_account', 'employee')
    return models().FundingDraft(**{
        'legal_entity_id': 'e', 'scenario': scenario, 'reason': '资金业务', 'amount': amount,
        'due_date': '2026-11-01', 'supplier_ref': funding_ref('supplier', 'supplier'),
        'business_refs': [funding_ref('contract', 'contract')], 'evidence_ids': ['material'],
        'authority': authority, **changes})


async def preview(store: Any, who: Any, doc: Any,
                  action: str = 'submit_funding_case', **parameters: Any) -> dict[str, Any]:
    """专用单据仍走唯一不可变预检和人工执行入口。"""
    return await store.preview_finance_action(who, ActionPreview(
        action=action, target_type='funding_case', target_id=doc['id'],
        expected_version=doc['version'], parameters=parameters),
        action + doc['id'] + str(doc['version']) + who.user_id + uuid4().hex[:12])


async def submit(store: Any, who: Any, doc: Any) -> dict[str, Any]:
    """实际人工提交后恢复最新工作流版本。"""
    plan = await preview(store, who, doc)
    assert plan['executable'], plan['blockers']
    await execute(store, who, plan)
    return await store.get_funding_case(who, doc['id'])


async def approve(store: Any, owner: Any, approver: Any, doc: Any) -> dict[str, Any]:
    """只让当前实际分派人工记录决定。"""
    queue = await store.list_review_queue(approver, 'e')
    step = next(x for x in queue['items'] if x['target_id'] == doc['id'])
    plan = await preview(store, approver, doc, 'approve',
                         workflow_step_id=step['workflow_step_id'])
    assert plan['executable'], plan['blockers']
    await execute(store, approver, plan)
    return await store.get_funding_case(owner, doc['id'])


def test_funding_strict_fields_and_scenario_authority() -> None:
    """模型不能注入核实状态，日期和场景专属声明保持严格。"""
    body = draft().model_dump()
    assert set(body) == {'legal_entity_id', 'scenario', 'reason', 'amount', 'due_date',
                         'supplier_ref', 'business_refs', 'evidence_ids', 'source_case_id', 'authority'}
    for fields in ({'verified': True}, {'amount': '0.00'}, {'due_date': '2026-2-01'},
                   {'authority': None}, {'source_case_id': 'parent'}):
        with pytest.raises(ValidationError):
            models().FundingDraft(**{**body, **fields})
    with pytest.raises(ValidationError):
        models().FundingAuthoritySpec(**{**body['authority'], 'verified': True})


async def test_prepayment_no_invoice_isolated_and_real_human_approval(store: Any) -> None:
    """无票例外只属预付，旧入口隔离且人工决定才标内部核对来源。"""
    owner, _, _, approver = await setup_funding(store)
    doc = await store.create_funding_case(owner, draft(), 'create')
    assert doc['kind'] == 'funding_case'
    assert doc['allocation_ids'] == [] and doc['funding']['scenario'] == 'prepayment'
    for method in (store.get_expense_claim, store.get_payable):
        with pytest.raises(FinanceError, match='RESOURCE_NOT_FOUND'):
            await method(owner, doc['id'])
    assert (await store.list_expense_claims(owner, 'e'))['total'] == 0
    assert (await store.list_payables(owner, 'e'))['total'] == 0
    assert (await store.list_funding_cases(owner, 'e'))['total'] == 1
    plan = await preview(store, owner, doc)
    with pytest.raises(FinanceError, match='HUMAN_REQUIRED'):
        await execute(store, replace(owner, origin='agent'), plan)
    doc = await submit(store, owner, doc)
    doc = await approve(store, owner, approver, doc)
    assert doc['status'] == 'approved'
    assert doc['funding']['internal_attestation']['source'] == 'finance_decision'
    assert doc['paid_amount'] == '0.00'
    assert await store.pool.fetchval('SELECT count(*) FROM finance_invoice') == 0


async def test_missing_policy_and_quarantined_original_rejected(store: Any) -> None:
    """不可见或未成功收件的URL和回执不能成为资金资格。"""
    owner, _, _, _ = await configured(store)
    assert hasattr(store, 'create_funding_case'), '缺少F5专用领域入口'
    with pytest.raises(FinanceError, match='FUNDING_POLICY_REQUIRED'):
        await store.create_funding_case(owner, draft(), 'missing-policy')
    await store.configure_funding([policy()])
    await funding_catalog(store)
    await material(store)
    await store.pool.execute("UPDATE finance_item SET state='quarantined'")
    with pytest.raises(FinanceError, match='FUNDING_EVIDENCE_INVALID'):
        await store.create_funding_case(owner, draft(), 'quarantined')


def funding_ref(kind: str, id: str) -> dict[str, str]:
    """资金目录明确提供买方销方版本，避免缺元数据被默认放行。"""
    return {**ref(kind, id), 'version': 'v2'}


async def funding_catalog(store: Any, *, provider: dict[str, Any] | None = None) -> None:
    """保留F2隔离制度，注入有真实主体信息的新版采购和员工账户。"""
    raw = decode(await store.pool.fetchval(
        'SELECT data FROM finance_settlement_policy_revision ORDER BY version DESC LIMIT 1'))
    raw['enabled'] = True
    if provider is not None:
        raw.update(version=raw['version'] + 1,
                   payment_configuration_version=provider['configuration_version'],
                   posting_configuration_version=provider['configuration_version'])
    documents = [BusinessSnapshot(tenant_id='t', legal_entity_id='e',
        ref=funding_ref(kind, id), name=id, verification_state='verified',
        allowed_user_ids=['owner'], party_tax_id='SELLER' if kind == 'supplier' else 'BUYER',
        counterparty_tax_id=None if kind == 'supplier' else 'SELLER')
        for kind, id in [('supplier', 'supplier'), ('contract', 'contract')]]
    accounts = [PayeeAccount(tenant_id='t', legal_entity_id='e',
        ref=ref(kind, id), subject_id=subject, account_name=id, masked_account='****0001',
        provider_account_id=id, verified=True, allowed_user_ids=['owner', 'payer'])
        for kind, id, subject in [('supplier_account', 'account', 'supplier'),
            ('employee_account', 'employee', 'owner'),
            ('supplier_account', 'entrusted', 'third-party')]]
    existing = decode(await store.pool.fetchval(
        "SELECT data FROM finance_settlement_catalog_revision WHERE kind='supplier_account' AND id='account'"))
    accounts[0] = PayeeAccount.model_validate(existing)
    if provider is None:
        provider = decode(await store.pool.fetchval('SELECT data FROM finance_settlement_provider'))
        provider['enabled'] = True
    await store.configure_settlements([SettlementPolicy.model_validate(raw)], documents, accounts,
                                      [provider], simulation=True)


async def test_funding_plan_exposes_material_subject_and_specific_internal_effects(store: Any) -> None:
    """确认卡提供原件和真实业务影响，不能把资金批准写成报销或银行核实。"""
    owner,_,_,approver=await setup_funding(store)
    doc=await submit(store,owner,await store.create_funding_case(owner,draft(),'c'))
    step=(await store.list_review_queue(approver,'e'))['items'][0]
    plan=await preview(store,approver,doc,'approve',workflow_step_id=step['workflow_step_id'])
    assert plan['funding']['scenario']=='prepayment'
    assert plan['funding']['owner_id']==owner.user_id
    assert plan['funding']['evidence'][0]['id']=='material'
    assert all('报销' not in text for text in plan['effects'])
    assert any('内部' in text for text in plan['effects'])


async def test_funding_sql_permission_pagination_and_cross_kind_revision(store: Any) -> None:
    """审批人仅看实际分派，SQL总数与页大小独立，旧应付修订入口隔离。"""
    from xuanpu_finance.expenses.models import PayableDraft, PayableUpdate
    owner,reviewer,_,approver=await setup_funding(store)
    docs=[await store.create_funding_case(owner,draft(amount='400.00'),key) for key in ('first','second')]
    assert (await store.list_funding_cases(approver,'e'))['total']==0
    await submit(store,owner,docs[0])
    assert (await store.list_funding_cases(approver,'e'))['total']==1
    first=await store.list_funding_cases(reviewer,'e',limit=1,scenario='prepayment')
    assert first['total']==2 and len(first['items'])==1 and first['next_cursor']
    second=await store.list_funding_cases(reviewer,'e',cursor=first['next_cursor'],limit=1,scenario='prepayment')
    assert second['total']==2 and first['items'][0]['id']!=second['items'][0]['id']
    with pytest.raises(FinanceError):
        await store.list_funding_cases(reviewer,'e',cursor=first['next_cursor'],limit=1,scenario='employee_advance')
    paydraft=PayableDraft(**draft().model_dump(exclude={'scenario','source_case_id','authority'}))
    with pytest.raises(FinanceError,match='RESOURCE_NOT_FOUND'):
        await store.update_payable(owner,docs[1]['id'],PayableUpdate(expected_version=1,draft=paydraft),'old-entry')


async def test_funding_rejects_original_sha_or_environment_conflict(store: Any) -> None:
    """文件URL不能替代实际原件，SHA及原接收环境不一致均拒绝。"""
    owner,_,_,_=await setup_funding(store)
    await store.pool.execute("UPDATE finance_item SET actual_sha256=$1",'f'*64)
    with pytest.raises(FinanceError,match='FUNDING_EVIDENCE_INVALID'):
        await store.create_funding_case(owner,draft(),'bad-sha')
    await store.pool.execute("UPDATE finance_item SET actual_sha256=$1",'b'*64)
    await store.pool.execute('UPDATE finance_operation SET source_simulation=false')
    with pytest.raises(FinanceError,match='FUNDING_EVIDENCE_INVALID'):
        await store.create_funding_case(owner,draft(),'bad-env')
