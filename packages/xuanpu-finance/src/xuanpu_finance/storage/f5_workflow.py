"""F5复用实际人工审批，原凭据和预付余额在同事务预留与收尾。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.funding.models import CLOSING, INVOICE_SCENARIOS, FundingDraft
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f5_documents import FundingDocumentStore

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionPreview, AllocationRequest
    from xuanpu_finance.storage.business_base import Connection


class FundingWorkflowStore(FundingDocumentStore):
    """F1当前指定人工链与F5共享责任始终在同一PG事务推进。"""

    def _action_effects(self, action: str) -> list[str]:
        """专用提交准确披露原凭据或原预付余额预留。"""
        if action == 'submit_funding_case':
            return ['预留原凭据额度及适用的原预付余额/发票额度，开始专用人工审批；不代表付款']
        return super()._action_effects(action)

    async def _funding_dependencies(self, conn: Connection, claim: Any) -> dict[str, Any]:
        """完整冻结基础制度、资金制度、材料、目录和原授权版本代数。"""
        owner = FinancePrincipal(claim['tenant_id'], claim['owner_id'])
        draft = FundingDraft.model_validate(decode(claim['content']))
        documents = await self._validate_funding_draft(conn, owner, draft)
        policy = await self._funding_policy(conn, claim['tenant_id'], claim['legal_entity_id'], require=False)
        base = await self._policy(conn, claim['tenant_id'], claim['legal_entity_id'], require=False)
        authority = await self._funding_authority(conn, claim)
        account = None
        if draft.authority is not None and draft.authority.account_ref is not None:
            account = await self._catalog_ref(conn, owner, claim['legal_entity_id'], draft.authority.account_ref)
        invoice_evidence = await self._funding_invoice_originals(conn, claim, base) if draft.scenario in INVOICE_SCENARIOS and base is not None else []
        source = None
        if draft.source_case_id is not None:
            parent = await self._funding_parent(conn, owner, draft)
            source = {'id': parent['id'], 'version': parent['version'], 'content': decode(parent['content']), 'evidence': await self._funding_evidence(conn,owner,parent['legal_entity_id'],decode(parent['content'])['evidence_ids'])}
        return {'policy': policy, 'base_policy': base, 'business_documents': documents, 'invoice_evidence': invoice_evidence, 'source_case': source,
                'evidence': await self._funding_evidence(conn, owner, claim['legal_entity_id'], draft.evidence_ids),
                'authority': None if authority is None else {k: authority[k] for k in ('id','version','revoked','body_digest','evidence_digest')}, 'account': account}

    async def _action_bindings(self, conn: Connection, who: FinancePrincipal, claim: Any, body: ActionPreview) -> dict[str, Any]:
        """专用计划绑定真实依赖，撤回仍可在制度停用后清理预留。"""
        bindings = await super()._action_bindings(conn, who, claim, body)
        if claim['document_kind'] != 'funding_case':
            return bindings
        try:
            bindings['funding'] = await self._funding_dependencies(conn, claim)
        except FinanceError as exc:
            bindings['funding'] = {'blocker': exc.code}
        policy = await self._funding_policy(conn, who.tenant_id, claim['legal_entity_id'], require=False)
        users = {who.user_id}
        if policy is not None:
            users.update(s['approver_user_id'] for s in policy['steps'][decode(claim['content'])['scenario']])
        content=decode(claim['content'])
        authority=await self._funding_authority(conn,claim)
        basis={'prepayment':'prepayment_basis','prepayment_clearance':'clearance_basis','prepayment_return':'internal_return_reconciliation'}.get(content['scenario'],'invoice')
        bindings['funding_summary']={'scenario':content['scenario'],'source_case_id':content.get('source_case_id'),
            'supplier_ref':content['supplier_ref'],'owner_id':claim['owner_id'],
            'evidence':await self._claim_evidence(conn,claim),'authority_id':authority['id'] if authority else None,'accounting_basis':basis}
        bindings['funding_authorities'] = [dict(r) for r in await conn.fetch('SELECT user_id,role,generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=ANY($3::text[]) ORDER BY user_id', who.tenant_id, claim['legal_entity_id'], sorted(users))]
        return bindings

    def _public_plan(self, payload: dict[str, Any], digest: str, blockers: list[str]) -> dict[str, Any]:
        """资金确认卡明确主体、实际原件和收尾影响，不冒充报销或银行核实。"""
        result=super()._public_plan(payload,digest,blockers)
        bindings=payload['bindings']
        summary=bindings.get('business',bindings).get('funding_summary')
        if summary is None:
            return result
        result['funding']=summary
        scenario=summary['scenario']
        action=payload['action']
        if action=='approve':
            effect={'prepayment_clearance':'提交预付核销并消费实际发票额度，减少原预付未结余额',
                    'prepayment_return':'记录内部退款核对并减少原预付未结余额；不生成银行到账事实'}.get(scenario,'形成资金业务及材料的内部人工核对批准，仍需独立付款或记账审批')
            result['effects']=['记录本人内部核对决定；最后一步通过后'+effect]
        elif action in {'reject','withdraw'}:
            result['effects']=['取消余下人工步骤并释放原凭据、原预付及适用的发票预留；保留实际历史']
        return result

    async def _funding_steps(self, conn: Connection, claim: Any, policy: dict[str, Any]) -> list[dict[str, Any]]:
        """按当前场景和金额门槛指定人员，空链和自审拒绝。"""
        scoped = {**policy, 'steps': policy['steps'][decode(claim['content'])['scenario']]}
        return await self._applicable_steps(conn, claim, scoped)

    async def _validate_action(self, conn: Connection, who: FinancePrincipal, claim: Any,
                               body: ActionPreview, *, lock: bool) -> list[str]:
        """专用链绑定F5制度及基础事实；普通动作完整透传。"""
        if claim['document_kind'] != 'funding_case':
            return await super()._validate_action(conn, who, claim, body, lock=lock)
        if body.target_type != 'funding_case':
            raise FinanceError('TARGET_KIND_MISMATCH', 422, '资金动作目标类型不符')
        if claim['version'] != body.expected_version:
            raise FinanceError('VERSION_CONFLICT', 409, '资金单据版本已变化')
        scenario = decode(claim['content'])['scenario']
        if scenario in CLOSING and claim['status'] == 'approved':
            raise FinanceError('FUNDING_FINALIZED', 409, '最终核销或退款核对事实不可撤回、驳回或修订')
        if body.action == 'withdraw':
            await super()._validate_action(conn, who, claim, body, lock=lock)
            return []
        if body.action == 'reject':
            return await self._funding_reject_check(conn, who, claim, body, lock=lock)
        policy = await self._funding_policy(conn, who.tenant_id, claim['legal_entity_id'])
        assert policy is not None
        await self._check_funding_authority(conn, claim, lock=lock)
        dependencies = await self._funding_dependencies(conn, claim)
        base = dependencies['base_policy']
        if base is None or base['record_mode'] != 'invoice_backed':
            raise FinanceError('INTERNAL_RECORD_NOT_EXECUTABLE', 409, '专用资金必须基于有效发票制度，内部台账不可提升执行资格')
        if body.action == 'submit_funding_case':
            self._owner_edit(who, claim, body.expected_version)
            steps = await self._funding_steps(conn, claim, policy)
            await self._reservation_check(conn, claim, base, lock=lock)
            await self._funding_balance_check(conn, claim, lock=lock)
            return [s['approver_user_id'] for s in steps]
        if body.action != 'approve':
            raise FinanceError('FUNDING_ACTION_INVALID', 422, '资金单据动作不适用')
        workflow, steps = await self._workflow_state(conn, claim, lock=lock)
        await self._validate_review(conn, who, claim, body, policy, workflow, steps)
        await self._funding_approved_dependencies(conn, claim, workflow, dependencies)
        await self._reserved_check(conn, claim, base, lock=lock)
        await self._funding_balance_check(conn, claim, lock=lock)
        return []

    async def _funding_reject_check(self, conn: Connection, who: FinancePrincipal, claim: Any,
                                     body: ActionPreview, *, lock: bool) -> list[str]:
        """原件撤销或制度停用后，当前实际分派者仍可驳回释放而不能新批准。"""
        workflow, steps = await self._workflow_state(conn, claim, lock=lock)
        if workflow is None:
            raise FinanceError('WORKFLOW_NOT_ACTIVE',409,'资金流程尚未提交')
        frozen = {'version':workflow['policy_version'],'digest':workflow['policy_digest'],
                  'generation':workflow['policy_generation']}
        await self._validate_review(conn,who,claim,body,frozen,workflow,steps)
        return []

    async def _funding_approved_dependencies(self, conn: Connection, claim: Any,
                                             workflow: Any, dependencies: dict[str, Any]) -> None:
        """后续实际批准及发送重审原业务批准时冻结的全部事实。"""
        if workflow is None or workflow['target_digest'] != request_digest(dependencies):
            raise FinanceError('FUNDING_DEPENDENCIES_CHANGED', 409, '资金制度、账户、材料或原采购版本已变化')
        if workflow['status']=='under_review' and workflow['due_at'] <= datetime.now(UTC):
            raise FinanceError('WORKFLOW_OVERDUE', 409, '资金审批已逾期，请撤回重提')

    async def _funding_balance_check(self, conn: Connection, claim: Any, *, lock: bool) -> None:
        """同责任锁下检查原件共享限额及核销退回累计父已付余额。"""
        authority = await self._check_funding_authority(conn, claim, lock=lock)
        if authority is not None:
            used = await conn.fetchval("SELECT COALESCE(sum(amount),0) FROM finance_funding_reservation WHERE tenant_id=$1 AND legal_entity_id=$2 AND authority_id=$3 AND state IN ('reserved','completed') AND document_id<>$4", claim['tenant_id'], claim['legal_entity_id'], authority['id'], claim['id'])
            if used+claim['total'] > Decimal(decode(authority['data'])['amount_limit']):
                raise FinanceError('FUNDING_AUTHORITY_BALANCE_EXCEEDED', 409, '原凭据共享额度不足')
        draft = FundingDraft.model_validate(decode(claim['content']))
        if draft.scenario in CLOSING:
            owner = FinancePrincipal(claim['tenant_id'], claim['owner_id'])
            parent = await self._funding_parent(conn, owner, draft)
            used = await conn.fetchval("SELECT COALESCE(sum(amount),0) FROM finance_funding_reservation WHERE tenant_id=$1 AND legal_entity_id=$2 AND source_case_id=$3 AND state IN ('reserved','completed') AND document_id<>$4", claim['tenant_id'], claim['legal_entity_id'], parent['id'], claim['id'])
            totals = await self._settlement_totals(conn, parent)
            if used+claim['total'] > Decimal(totals['paid_amount']):
                raise FinanceError('PREPAYMENT_BALANCE_EXCEEDED', 409, '核销和退回累计超过已确定付款余额')

    async def _submit_claim(self, conn: Connection, who: FinancePrincipal, claim: Any) -> None:
        """专用提交复用F1表和人工链，同时原子冻结共享责任与票据预留。"""
        if claim['document_kind'] != 'funding_case':
            await super()._submit_claim(conn, who, claim)
            return
        policy = await self._funding_policy(conn, who.tenant_id, claim['legal_entity_id'])
        assert policy is not None
        steps = await self._funding_steps(conn, claim, policy)
        dependencies = await self._funding_dependencies(conn, claim)
        await self._funding_balance_check(conn, claim, lock=True)
        await conn.execute("UPDATE finance_allocation SET state='reserved',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='proposed'", who.tenant_id, claim['legal_entity_id'], claim['id'])
        claim = await self._bump_claim(conn, claim, status='under_review')
        workflow = identifier()
        due = datetime.now(UTC)+timedelta(hours=dependencies['base_policy']['approval_due_hours'])
        await conn.execute("INSERT INTO finance_workflow(tenant_id,legal_entity_id,id,document_id,target_version,status,policy_version,policy_digest,due_at,policy_generation,target_type,target_digest) VALUES($1,$2,$3,$4,$5,'under_review',$6,$7,$8,$9,'funding_case',$10)", who.tenant_id, claim['legal_entity_id'], workflow, claim['id'], claim['version'], policy['version'], policy['digest'], due, policy['generation'], request_digest(dependencies))
        for position, step in enumerate(steps):
            await conn.execute("INSERT INTO finance_workflow_step(tenant_id,legal_entity_id,id,workflow_id,position,approver_user_id,name,status,authority_generation) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)", who.tenant_id, claim['legal_entity_id'], identifier(), workflow, position, step['approver_user_id'], step['name'], 'pending' if position==0 else 'waiting', await self._current_approver(conn, claim, step['approver_user_id']))
        binding = await conn.fetchrow('SELECT * FROM finance_funding_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3', who.tenant_id, claim['legal_entity_id'], claim['id'])
        assert binding is not None
        await conn.execute("INSERT INTO finance_funding_reservation(tenant_id,legal_entity_id,document_id,authority_id,source_case_id,kind,amount,state) VALUES($1,$2,$3,$4,$5,$6,$7,'reserved') ON CONFLICT(tenant_id,legal_entity_id,document_id) DO UPDATE SET authority_id=EXCLUDED.authority_id,source_case_id=EXCLUDED.source_case_id,amount=EXCLUDED.amount,state='reserved',decision_id=NULL", who.tenant_id, claim['legal_entity_id'], claim['id'], binding['authority_id'], binding['source_case_id'], decode(claim['content'])['scenario'], claim['total'])
        await self._funding_financial_bump(conn, claim)
        await self._save_history(conn, claim)
        await self._event(conn, claim, 'funding.submitted', {'workflow_id': workflow, 'version': claim['version']})
        await self._event(conn, claim, 'expense.under_review', {'workflow_id': workflow})

    async def _apply_action(self, conn: Connection, who: FinancePrincipal, claim: Any, payload: dict[str, Any]) -> None:
        """只增加专用提交名，其余实际人工执行由原F1引擎处理。"""
        if payload['action'] == 'submit_funding_case':
            await self._submit_claim(conn, who, claim)
            return
        await super()._apply_action(conn, who, claim, payload)

    async def _save_history(self, conn: Connection, row: Any) -> dict[str, Any]:
        """最终资金事实先完成再存不可变历史，避免快照落后于同事务余额。"""
        if row['document_kind']=='funding_case':
            if row['status']=='approved':
                await self._finalize_funding(conn, row)
            elif row['status'] in {'rejected','withdrawn'}:
                await conn.execute("UPDATE finance_funding_reservation SET state='released' WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='reserved'",row['tenant_id'],row['legal_entity_id'],row['id'])
                await self._funding_financial_bump(conn,row)
        return await super()._save_history(conn, row)

    async def _finalize_funding(self, conn: Connection, claim: Any) -> None:
        """最后实际批准形成内部核销/退款，资金批准仍保留共享责任占额。"""
        scenario = decode(claim['content'])['scenario']
        if scenario in CLOSING:
            decision = await conn.fetchval("SELECT d.id FROM finance_decision d JOIN finance_workflow w ON(w.tenant_id=d.tenant_id AND w.legal_entity_id=d.legal_entity_id AND w.id=d.workflow_id) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.document_id=$3 AND w.target_type='funding_case' AND w.status='approved' AND d.action='approve' ORDER BY d.created_at DESC,d.id DESC LIMIT 1", claim['tenant_id'], claim['legal_entity_id'], claim['id'])
            if decision is None:
                raise FinanceError('ACTUAL_DECISION_REQUIRED',409,'内部核销或退款必须来自实际人工最终决定')
            await conn.execute("UPDATE finance_funding_reservation SET state='completed',decision_id=$4 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='reserved'", claim['tenant_id'], claim['legal_entity_id'], claim['id'], decision)
            await conn.execute("UPDATE finance_allocation SET state='consumed',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='reserved'", claim['tenant_id'], claim['legal_entity_id'], claim['id'])
            await conn.execute("UPDATE finance_execution_grant SET active=false WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND scope='expense_approved'", claim['tenant_id'], claim['legal_entity_id'], claim['id'])
        await self._funding_financial_bump(conn, claim)
        await self._event(conn, claim, 'funding.'+('reconciled' if scenario in CLOSING else 'approved'), {'internal_attestation':'finance_decision'})

    async def _close_workflow(self, conn: Connection, who: FinancePrincipal, claim: Any, workflow: Any,
                              steps: list[Any], payload: dict[str, Any]) -> None:
        """待审或未执行撤回释放两侧预留，已完成收尾不会走到本分支。"""
        await super()._close_workflow(conn, who, claim, workflow, steps, payload)
        if claim['document_kind'] == 'funding_case':
            await self._event(conn, claim, 'funding.reservation_released', {})

    async def _funding_financial_bump(self, conn: Connection, claim: Any) -> None:
        """父和子财务版本同步递增而不改历史申报内容版本。"""
        await conn.execute('UPDATE finance_funding_case SET financial_version=financial_version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND (document_id=$3 OR document_id=(SELECT source_case_id FROM finance_funding_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3))', claim['tenant_id'], claim['legal_entity_id'], claim['id'])

    async def create_invoice_allocation(self, who: FinancePrincipal, body: AllocationRequest, key: str) -> dict[str, Any]:
        """无票例外单据不能通过随意分配制造发票费用资格。"""
        if body.document_kind == 'funding_case':
            async with self.pool.acquire() as conn:
                doc = await self._funding_row(conn, who, body.document_id)
                if decode(doc['content'])['scenario'] not in INVOICE_SCENARIOS:
                    raise FinanceError('FUNDING_ALLOCATION_FORBIDDEN', 409, '预付和内部退回不能附造发票分配')
        return await super().create_invoice_allocation(who, body, key)

    async def _allocation_invoice(self, conn: Connection, claim: Any, invoice: Any, version: int) -> dict[str, Any]:
        """资金发票保留原买方/票态约束，并核对供应商销方与来源环境。"""
        data = await super()._allocation_invoice(conn, claim, invoice, version)
        if claim['document_kind'] == 'funding_case':
            draft = FundingDraft.model_validate(decode(claim['content']))
            owner = FinancePrincipal(claim['tenant_id'], claim['owner_id'])
            supplier = await self._catalog_ref(conn, owner, claim['legal_entity_id'], draft.supplier_ref)
            if invoice['seller_tax_id'] != supplier.get('party_tax_id'):
                raise FinanceError('INVOICE_SELLER_MISMATCH', 409, '发票销方与原供应商不符')
            if invoice['source_simulation'] != claim['simulation']:
                raise FinanceError('SIMULATION_MODE_CONFLICT', 409, '票据原接收环境与资金单据不符')
        return data

    async def _reservation_check(self, conn: Connection, claim: Any, policy: dict[str, Any], *, lock: bool) -> list[dict[str, Any]]:
        """仅预付及有原件退回享无票例外；其他场景完整保留F1票据约束。"""
        if claim['document_kind']=='funding_case' and decode(claim['content'])['scenario'] not in INVOICE_SCENARIOS:
            return []
        result = await super()._reservation_check(conn, claim, policy, lock=lock)
        if claim['document_kind']=='funding_case':
            await self._funding_invoice_originals(conn, claim, policy)
        return result

    async def _reserved_check(self, conn: Connection, claim: Any, policy: dict[str, Any], *, lock: bool) -> None:
        """预付无票不制造票据验证；正式发票场景沿用所有既有硬约束。"""
        if claim['document_kind']=='funding_case' and decode(claim['content'])['scenario'] not in INVOICE_SCENARIOS:
            return
        await super()._reserved_check(conn, claim, policy, lock=lock)
        if claim['document_kind']=='funding_case':
            await self._funding_invoice_originals(conn, claim, policy)

    async def _funding_invoice_originals(self, conn: Connection, doc: Any, policy: dict[str, Any]) -> list[dict[str, Any]]:
        """每张提议、预留或已核销发票均须有实际有效原件，模拟也不省略原件。"""
        rows = await conn.fetch("SELECT DISTINCT i.id,i.data FROM finance_allocation a JOIN finance_invoice i ON(i.tenant_id=a.tenant_id AND i.legal_entity_id=a.legal_entity_id AND i.id=a.invoice_id) WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.document_id=$3 AND a.state IN ('proposed','reserved','consumed')",doc['tenant_id'],doc['legal_entity_id'],doc['id'])
        evidence=[]
        owner=FinancePrincipal(doc['tenant_id'],doc['owner_id'])
        for row in rows:
            originals=await conn.fetch('SELECT receipt_id FROM finance_invoice_receipt WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 ORDER BY receipt_id',doc['tenant_id'],doc['legal_entity_id'],row['id'])
            if not originals:
                raise FinanceError('INVOICE_EVIDENCE_REQUIRED',409,'专用资金每张发票必须保全实际原件')
            evidence.extend(await self._funding_evidence(conn,owner,doc['legal_entity_id'],[r['receipt_id'] for r in originals]))
            if not doc['simulation'] or policy['require_verified_invoice']:
                await self._funding_current_verification(conn,doc,decode(row['data']))
        return evidence

    async def _funding_current_verification(self,conn: Connection,doc: Any,data: dict[str,Any]) -> None:
        """正式票据资格要求当前渠道原始查验及期限，passed字符串不足以放行。"""
        from xuanpu_finance.storage.f3_projection import (
            invoice_projection,
            require_usable_projection,
        )
        data=await invoice_projection(conn,doc['tenant_id'],doc['legal_entity_id'],data)
        require_usable_projection(data)
        evidence=data.get('verification_evidence')
        if data.get('verification_status')!='passed' or not isinstance(evidence,dict):
            raise FinanceError('VALID_VERIFICATION_REQUIRED',409,'正式资金业务须有当前有效查验渠道证据')
        try:
            until=datetime.fromisoformat(evidence['valid_until'])
        except (KeyError,TypeError,ValueError) as exc:
            raise FinanceError('VALID_VERIFICATION_REQUIRED',409,'正式查验期限无效') from exc
        if until.tzinfo is None or until<=datetime.now(UTC) or not evidence.get('provider') or not evidence.get('raw_response_ref'):
            raise FinanceError('VALID_VERIFICATION_REQUIRED',409,'正式查验证据缺失或已过期')
