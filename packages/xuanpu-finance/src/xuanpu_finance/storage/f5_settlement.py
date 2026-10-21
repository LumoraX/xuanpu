"""专用资金只扩展F2资格和请求上下文，复用原队列/查单/fencing。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.funding.models import CLOSING, INVOICE_SCENARIOS
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f5_workflow import FundingWorkflowStore

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionPreview
    from xuanpu_finance.storage.business_base import Connection


class FundingStore(FundingWorkflowStore):
    """普通F2所有路径继续原实现，专用场景不获得任何隐式通道资格。"""

    async def _external_prerequisites(self, conn: Connection, who: FinancePrincipal, doc: Any,
                                      body: ActionPreview, *, lock: bool) -> tuple[dict[str, Any], list[Any]]:
        """先验证原资金业务批准和不可变事实，再要求原F2专用岗位分离。"""
        if doc['document_kind'] != 'funding_case':
            return await super()._external_prerequisites(conn, who, doc, body, lock=lock)
        if body.target_type != 'funding_case' or body.expected_version != doc['version']:
            raise FinanceError('VERSION_CONFLICT', 409, '资金目标类型或版本已变化')
        scenario = decode(doc['content'])['scenario']
        if body.action == 'execute_payment' and scenario in CLOSING:
            raise FinanceError('FUNDING_PAYMENT_FORBIDDEN', 409, '核销和退款核对只能收尾，禁止再次对外付款')
        if body.action == 'reverse_posting':
            raise FinanceError('FUNDING_CORRECTION_NOT_SUPPORTED', 409, '本期不支持跨已记账期间的资金更正写操作')
        if doc['status'] != 'approved' or doc['simulation'] != await self._simulation(conn):
            raise FinanceError('BUSINESS_APPROVAL_REQUIRED', 409, '专用资金须有本环境实际业务批准')
        policy = await self._settlement_policy(conn, who.tenant_id, doc['legal_entity_id'])
        funding = await self._funding_policy(conn, who.tenant_id, doc['legal_entity_id'])
        assert policy is not None and funding is not None
        payment = body.action == 'execute_payment'
        executors = policy['payment_executor_user_ids' if payment else 'posting_executor_user_ids']
        if who.user_id == doc['owner_id'] or who.user_id not in executors or await self._role(conn, who, doc['legal_entity_id']) != 'reviewer':
            raise FinanceError('EXECUTOR_FORBIDDEN', 403, '须由与申请人分离的当前专用资金或记账经办执行')
        workflow, steps = await self._workflow_state(conn, doc, lock=lock)
        if workflow is None or workflow['status'] != 'approved':
            raise FinanceError('BUSINESS_APPROVAL_REQUIRED', 409, '原专用资金批准无效')
        if (workflow['policy_version'],workflow['policy_digest'],workflow['policy_generation']) != (funding['version'],funding['digest'],funding['generation']):
            raise FinanceError('POLICY_CHANGED', 409, '原资金制度已变更')
        await self._check_funding_authority(conn, doc, lock=lock)
        dependencies = await self._funding_dependencies(conn, doc)
        await self._funding_approved_dependencies(conn, doc, workflow, dependencies)
        for step in steps:
            await self._current_approver(conn, doc, step['approver_user_id'], step['authority_generation'])
        await self._check_funding_authority(conn, doc, lock=lock)
        await self._funding_balance_check(conn, doc, lock=lock)
        await self._reserved_check(conn, doc, dependencies['base_policy'], lock=lock)
        await self._external_invoice_evidence(conn, doc, policy)
        await self._funding_business_grant(conn, doc, funding, scenario)
        return policy, steps

    async def _funding_business_grant(self, conn: Connection, doc: Any, funding: dict[str, Any], scenario: str) -> None:
        """付款场景必须保留匹配F5制度的实际业务批准授权。"""
        if scenario not in CLOSING:
            grant = await conn.fetchrow("SELECT * FROM finance_execution_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND scope='expense_approved' AND target_version=$4 AND active", doc['tenant_id'], doc['legal_entity_id'], doc['id'], doc['version'])
            if grant is None or (grant['policy_version'],grant['policy_digest'],grant['policy_generation']) != (funding['version'],funding['digest'],funding['generation']):
                raise FinanceError('BUSINESS_APPROVAL_REQUIRED', 409, '原资金批准授权缺失或失效')

    async def _external_provider(self, conn: Connection, who: FinancePrincipal, doc: Any,
                                 policy: dict[str, Any], *, payment: bool) -> dict[str, Any]:
        """旧通道默认没有资金场景资格，不能自动获得新能力。"""
        provider = await super()._external_provider(conn, who, doc, policy, payment=payment)
        if doc['document_kind'] == 'funding_case' and decode(doc['content'])['scenario'] not in provider.get('supported_funding_scenarios', []):
            raise FinanceError('FUNDING_PROVIDER_CAPABILITY_REQUIRED', 409, '渠道未显式声明支持当前资金场景')
        return provider

    async def _external_invoice_evidence(self, conn: Connection, doc: Any, policy: dict[str, Any]) -> None:
        """预付及退款使用明确业务依据；其余资金场景须有实际发票原件。"""
        if doc['document_kind'] != 'funding_case':
            await super()._external_invoice_evidence(conn, doc, policy)
            return
        if decode(doc['content'])['scenario'] not in INVOICE_SCENARIOS:
            return
        await super()._external_invoice_evidence(conn, doc, policy)
        await self._funding_invoice_originals(conn, doc, policy)

    async def _external_parameter_bindings(self, conn: Connection, who: FinancePrincipal, doc: Any,
                                           body: ActionPreview, policy: dict[str, Any]) -> dict[str, Any]:
        """仅员工原垫付和精确委托账户享专用分支，普通PAYEE_MISMATCH不放宽。"""
        scenario = decode(doc['content']).get('scenario') if doc['document_kind']=='funding_case' else None
        if body.action != 'execute_payment' or scenario not in {'employee_advance','entrusted_collection'}:
            return await super()._external_parameter_bindings(conn, who, doc, body, policy)
        authority = await self._check_funding_authority(conn, doc)
        declared = decode(authority['data'])['account_ref']
        assert body.parameters.payee_ref is not None
        if body.parameters.payee_ref.model_dump() != declared:
            raise FinanceError('PAYEE_MISMATCH', 409, '收款账户必须是原垫付员工本人或原授权明确指定账户')
        account = await self._catalog_ref(conn, who, doc['legal_entity_id'], body.parameters.payee_ref)
        kind = 'employee_account' if scenario=='employee_advance' else 'supplier_account'
        if not account['verified'] or account['ref']['kind']!=kind or (scenario=='employee_advance' and account['subject_id']!=doc['owner_id']):
            raise FinanceError('PAYEE_MISMATCH', 409, '原垫付员工本人或原授权账户不匹配')
        return {'payee': account}

    async def _external_bindings(self, conn: Connection, who: FinancePrincipal, doc: Any,
                                 body: ActionPreview, *, lock: bool,
                                 exclude_operation_id: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """固定场景、授权、原预付、材料摘要及记账依据进入批准与外发摘要。"""
        bindings, steps = await super()._external_bindings(conn, who, doc, body, lock=lock, exclude_operation_id=exclude_operation_id)
        if doc['document_kind']=='funding_case':
            content = decode(doc['content'])
            authority = await self._funding_authority(conn, doc)
            evidence = await self._funding_evidence(conn, FinancePrincipal(doc['tenant_id'], doc['owner_id']), doc['legal_entity_id'], content['evidence_ids'])
            basis = {'prepayment':'prepayment_basis','prepayment_clearance':'clearance_basis','prepayment_return':'internal_return_reconciliation'}.get(content['scenario'],'invoice')
            bindings['funding'] = {'scenario': content['scenario'], 'source_case_id':content.get('source_case_id'),
                'authority_id': authority['id'] if authority else None,
                'authority_reference': content.get('authority',{}).get('reference'),
                'evidence_digest':request_digest(evidence), 'accounting_basis':basis,
                'internal_attestation_source':'finance_decision'}
            bindings['funding_digest'] = request_digest(bindings['funding'])
        return bindings, steps

    def _dispatch_request(self, row: Any, plan: Any) -> dict[str, Any]:
        """原F2请求增加不可替换资金上下文后重新计算完整载荷摘要。"""
        request = super()._dispatch_request(row, plan)
        bindings = decode(plan['data'])['bindings']
        if 'funding' in bindings:
            request.update(funding=bindings['funding'], funding_digest=bindings['funding_digest'])
            request.pop('payload_hash')
            request['payload_hash'] = request_digest(request)
        return request

    async def _worker_lock(self, conn: Connection, job: dict[str, Any]) -> tuple[Any, Any]:
        """发送和结果确认也先取得公司资金责任锁，遵守人工动作同一锁序。"""
        row = await conn.fetchrow("SELECT document_kind FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", job['tenant_id'], job['legal_entity_id'], job['id'])
        if row is not None and row['document_kind']=='funding_case':
            await self._funding_company_lock(conn, job['tenant_id'], job['legal_entity_id'])
        return await super()._worker_lock(conn, job)

    async def _accept_external_action(self, conn: Connection, who: FinancePrincipal, doc: Any,
                                       plan: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """受理在途金额时同步增加独立资金版本，内容版本保持原批准目标。"""
        result=await super()._accept_external_action(conn,who,doc,plan,payload)
        if doc['document_kind']=='funding_case':
            await self._funding_financial_bump(conn,doc)
        return result

    async def _update_record_state(self, conn: Connection, row: Any, status: str, result: dict[str, Any]) -> None:
        """渠道资金状态变化增加财务版本，未知状态保留原资金占用。"""
        await super()._update_record_state(conn,row,status,result)
        if row['document_kind']=='funding_case':
            await conn.execute('UPDATE finance_funding_case SET financial_version=financial_version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3',row['tenant_id'],row['legal_entity_id'],row['document_id'])
