"""专用资金草稿和权限过滤查询，不混入报销或普通应付。"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.funding.models import CLOSING, SCENARIOS, FundingDraft
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_base import money
from xuanpu_finance.storage.common import (
    decode,
    decode_cursor,
    encode_cursor,
    identifier,
    page,
    timestamp_cursor,
)
from xuanpu_finance.storage.f5_authorities import FundingAuthorityStore

if TYPE_CHECKING:
    from xuanpu_finance.funding.models import FundingUpdate
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class FundingDocumentStore(FundingAuthorityStore):
    """所有专用单据仍由原claim/history/workflow持久化并按租户隔离。"""

    async def _funding_company_lock(self, conn: Connection, tenant: str, entity: str) -> None:
        """同公司资金责任与父余额采用统一事务锁序，跨公司可并行。"""
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', request_digest(['funding', tenant, entity]))

    async def _claim_row(self, conn: Connection, who: FinancePrincipal, id: str, *, lock: bool = False) -> Any:
        """资金锁先于claim/父单/原凭据/发票，普通单据仍走原有路径。"""
        if lock:
            scope = await conn.fetchrow("SELECT legal_entity_id FROM finance_expense_claim WHERE tenant_id=$1 AND id=$2 AND document_kind='funding_case'", who.tenant_id, id)
            if scope is not None:
                await self._funding_company_lock(conn, who.tenant_id, scope['legal_entity_id'])
        row=await super()._claim_row(conn, who, id, lock=lock)
        if row['document_kind']=='funding_case' and row['owner_id']!=who.user_id:
            if await self._role(conn,who,row['legal_entity_id'])!='reviewer' and not await self._funding_current_assignment(conn,who,row):
                raise FinanceError('RESOURCE_NOT_FOUND',404,'专用资金单据不存在')
        return row

    async def _funding_row(self, conn: Connection, who: FinancePrincipal, id: str, *, lock: bool = False) -> Any:
        """专用入口拒绝其他底表单据类型。"""
        row = await self._claim_row(conn, who, id, lock=lock)
        if row['document_kind'] != 'funding_case':
            raise FinanceError('RESOURCE_NOT_FOUND', 404, '专用资金单据不存在')
        return row

    async def _validate_funding_draft(self, conn: Connection, who: FinancePrincipal, body: FundingDraft) -> list[dict[str, Any]]:
        """受控采购资料及支持原件必须真实可见，买方与供应商保持一致。"""
        await self._policy(conn, who.tenant_id, body.legal_entity_id)
        snapshots = [await self._catalog_ref(conn, who, body.legal_entity_id, ref) for ref in [body.supplier_ref, *body.business_refs]]
        if any(d['verification_state'] != 'verified' for d in snapshots):
            raise FinanceError('BUSINESS_DOCUMENT_NOT_VERIFIED', 409, '资金采购依据尚未核实')
        buyer = await conn.fetchval('SELECT tax_id FROM finance_company WHERE tenant_id=$1 AND legal_entity_id=$2 FOR SHARE', who.tenant_id, body.legal_entity_id)
        seller = snapshots[0].get('party_tax_id')
        if not seller or any(d.get('party_tax_id') != buyer or d.get('counterparty_tax_id') != seller for d in snapshots[1:]):
            raise FinanceError('FUNDING_PARTY_MISMATCH', 409, '公司买方、供应商销方与采购合同主体不一致或资料不完整')
        await self._funding_evidence(conn, who, body.legal_entity_id, body.evidence_ids)
        if body.authority is not None:
            await self._funding_evidence(conn, who, body.legal_entity_id, body.authority.evidence_ids, supporting=True)
            if body.authority.account_ref is not None:
                account = await self._catalog_ref(conn, who, body.legal_entity_id, body.authority.account_ref)
                if not account['verified'] or (body.scenario == 'employee_advance' and account['subject_id'] != who.user_id):
                    raise FinanceError('PAYEE_MISMATCH', 409, '原垫付员工及其本人账户不匹配或账户未核实')
        if body.scenario in CLOSING:
            await self._funding_parent(conn, who, body)
        return snapshots

    async def _funding_parent(self, conn: Connection, who: FinancePrincipal, body: FundingDraft) -> Any:
        """核销退回仅能关联当前可见同主体同合同原版本的已付预付。"""
        assert body.source_case_id is not None
        parent = await self._funding_row(conn, who, body.source_case_id, lock=True)
        content = decode(parent['content'])
        if parent['owner_id'] != who.user_id:
            raise FinanceError('OWNER_REQUIRED', 403, '预付收尾须由原预付申请人本人申报')
        if (content['scenario'] != 'prepayment' or parent['legal_entity_id'] != body.legal_entity_id
            or content['supplier_ref'] != body.supplier_ref.model_dump()
            or sorted(content['business_refs'], key=request_digest) != sorted([r.model_dump() for r in body.business_refs], key=request_digest)):
            raise FinanceError('PREPAYMENT_PARENT_MISMATCH', 409, '原预付公司、供应商或合同原版本不一致')
        await self._funding_evidence(conn,who,parent['legal_entity_id'],content['evidence_ids'])
        totals = await self._settlement_totals(conn, parent)
        if Decimal(totals['paid_amount']) <= 0 or Decimal(totals['payment_pending_amount']) > 0:
            raise FinanceError('PREPAYMENT_PAYMENT_REQUIRED', 409, '必须先取得原预付确定付款，未知在途不可核销')
        if parent['simulation'] != await self._simulation(conn):
            raise FinanceError('SIMULATION_MODE_CONFLICT', 409, '原预付环境不匹配')
        return parent

    async def create_funding_case(self, who: FinancePrincipal, body: FundingDraft, key: str) -> dict[str, Any]:
        """创建严格专用草稿并建立不可变原件责任，保存不代表已核实。"""
        request = body.model_dump(mode='json', exclude_none=True)
        async with self.pool.acquire() as conn, conn.transaction():
            await self._funding_company_lock(conn, who.tenant_id, body.legal_entity_id)
            previous = await self._business_previous(conn, who, body.legal_entity_id, 'createFundingCase', key, request)
            if previous is not None:
                return previous
            await self._funding_policy(conn, who.tenant_id, body.legal_entity_id)
            await self._validate_funding_draft(conn, who, body)
            authority = await self._register_funding_authority(conn, who, body)
            mode = {'prepayment': 'prepayment_basis', 'prepayment_return': 'return_reconciliation'}.get(body.scenario, 'invoice_backed')
            row = await conn.fetchrow("INSERT INTO finance_expense_claim(tenant_id,legal_entity_id,id,owner_id,version,status,total,content,record_mode,document_kind,simulation) VALUES($1,$2,$3,$4,1,'draft',$5,$6::jsonb,$7,'funding_case',$8) RETURNING *", who.tenant_id, body.legal_entity_id, identifier(), who.user_id, Decimal(body.amount), json.dumps(request), mode, await self._simulation(conn))
            assert row is not None
            await conn.execute('INSERT INTO finance_funding_case(tenant_id,legal_entity_id,document_id,authority_id,source_case_id) VALUES($1,$2,$3,$4,$5)', who.tenant_id, body.legal_entity_id, row['id'], authority, body.source_case_id)
            await self._link_receipts(conn, row, body.evidence_ids)
            result = await self._save_history(conn, row)
            await self._event(conn, row, 'funding.created', {'version': 1})
            return await self._business_remember(conn, who, body.legal_entity_id, 'createFundingCase', key, request, result)

    async def update_funding_case(self, who: FinancePrincipal, id: str, body: FundingUpdate, key: str) -> dict[str, Any]:
        """CAS完整修订保留历史；公司、场景、原预付身份不可更换。"""
        request = {'id': id, **body.model_dump(mode='json', exclude_none=True)}
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._funding_row(conn, who, id, lock=True)
            previous = await self._business_previous(conn, who, row['legal_entity_id'], 'updateFundingCase', key, request)
            if previous is not None:
                return previous
            self._owner_edit(who, row, body.expected_version)
            old = decode(row['content'])
            if (body.draft.legal_entity_id != row['legal_entity_id'] or body.draft.scenario != old['scenario'] or body.draft.source_case_id != old.get('source_case_id')):
                raise FinanceError('FUNDING_IDENTITY_IMMUTABLE', 422, '资金公司、场景及原预付不可更换')
            await self._funding_policy(conn, who.tenant_id, row['legal_entity_id'])
            await self._settlement_withdraw_guard(conn, row)
            await self._validate_funding_draft(conn, who, body.draft)
            authority = await self._register_funding_authority(conn, who, body.draft)
            await conn.execute("UPDATE finance_allocation SET state='released',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='proposed'", who.tenant_id, row['legal_entity_id'], id)
            await conn.execute('UPDATE finance_funding_case SET authority_id=$4,financial_version=financial_version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3', who.tenant_id, row['legal_entity_id'], id, authority)
            row = await conn.fetchrow("UPDATE finance_expense_claim SET total=$4,content=$5::jsonb,version=version+1,status='draft' WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *", who.tenant_id, row['legal_entity_id'], id, Decimal(body.draft.amount), body.draft.model_dump_json(exclude_none=True))
            assert row is not None
            await self._link_receipts(conn, row, body.draft.evidence_ids)
            result = await self._save_history(conn, row)
            await self._event(conn, row, 'funding.revised', {'version': row['version']})
            return await self._business_remember(conn, who, row['legal_entity_id'], 'updateFundingCase', key, request, result)

    async def get_funding_case(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """恢复标准业务单据以及专用责任和余额投影。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read'):
            return await self._claim_snapshot(conn, await self._funding_row(conn, who, id))

    async def list_funding_cases(self, who: FinancePrincipal, entity: str, cursor: str | None = None,
                                 limit: int = 50, scenario: str | None = None, status: str | None = None) -> dict[str, Any]:
        """SQL权限、场景和状态过滤后独立计数并limit加一稳定续读。"""
        if scenario not in {None, *SCENARIOS} or status not in {None, 'draft', 'submitted', 'under_review', 'approved', 'rejected', 'withdrawn'}:
            raise FinanceError('FUNDING_FILTER_INVALID', 422, '专用资金筛选无效')
        scope = request_digest([who.tenant_id, who.user_id, entity, 'funding', scenario, status])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read'):
            role = await self._role(conn, who, entity)
            where = "c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.document_kind='funding_case' AND (c.owner_id=$3 OR $4::text='reviewer' OR ($4::text='approver' AND EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('approver','reviewer')) WHERE w.tenant_id=c.tenant_id AND w.legal_entity_id=c.legal_entity_id AND w.document_id=c.id AND s.approver_user_id=$3))) AND ($5::text IS NULL OR c.content->>'scenario'=$5) AND ($6::text IS NULL OR c.status=$6)"
            args = (who.tenant_id, entity, who.user_id, role, scenario, status)
            total = await conn.fetchval('SELECT count(*) FROM finance_expense_claim c WHERE '+where, *args)
            rows = await conn.fetch('SELECT c.* FROM finance_expense_claim c WHERE '+where+' AND ($7::timestamptz IS NULL OR (c.created_at,c.id)>($7,$8::text)) ORDER BY c.created_at,c.id LIMIT $9', *args, stamp, last, limit+1)
            visible = rows[:limit]
            return page([await self._claim_snapshot(conn, row) for row in visible], total,
                        encode_cursor(scope, [visible[-1]['created_at'].isoformat(), visible[-1]['id']]) if len(rows)>limit else None)

    async def _claim_snapshot(self, conn: Connection, row: Any) -> dict[str, Any]:
        """专用财务余额与内容版本分离，内部核对来源固定为实际决定。"""
        result = await super()._claim_snapshot(conn, row)
        if row['document_kind'] != 'funding_case':
            return result
        content = decode(row['content'])
        binding = await conn.fetchrow('SELECT * FROM finance_funding_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3', row['tenant_id'], row['legal_entity_id'], row['id'])
        assert binding is not None
        amounts = await self._funding_closing_totals(conn, row)
        decision = await conn.fetchrow("SELECT d.id,d.actor_id FROM finance_decision d JOIN finance_workflow w ON(w.tenant_id=d.tenant_id AND w.legal_entity_id=d.legal_entity_id AND w.id=d.workflow_id) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.document_id=$3 AND w.target_type='funding_case' AND w.status='approved' AND d.action='approve' ORDER BY d.created_at DESC,d.id DESC LIMIT 1", row['tenant_id'], row['legal_entity_id'], row['id'])
        attestation = None if decision is None else {'source': 'finance_decision', 'decision_id': decision['id'], 'actor_id': decision['actor_id'], 'action': 'approve'}
        result['funding'] = {'scenario': content['scenario'], 'source_case_id': content.get('source_case_id'),
            'authority': self._funding_authority_view(await self._funding_authority(conn, row)),
            'financial_version': binding['financial_version'], 'paid_amount': result['paid_amount'],
            'payment_pending_amount': result['payment_pending_amount'], **amounts,
            'internal_attestation': attestation}
        result['funding']['outstanding_amount'] = money(Decimal(result['paid_amount'])-Decimal(amounts['cleared_amount'])-Decimal(amounts['returned_amount'])) if content['scenario']=='prepayment' else '0.00'
        return result

    async def _funding_closing_totals(self, conn: Connection, row: Any) -> dict[str, str]:
        """已批准收尾与待审预留分别展示，不把内部退款标为银行入款。"""
        reservations = await conn.fetch("SELECT kind,state,amount FROM finance_funding_reservation WHERE tenant_id=$1 AND legal_entity_id=$2 AND (source_case_id=$3 OR document_id=$3) AND state<>'released'", row['tenant_id'], row['legal_entity_id'], row['id'])
        return {key: money(sum((r['amount'] for r in reservations if r['kind']==kind and r['state']==state), Decimal(0)))
                for key, kind, state in [('cleared_amount','prepayment_clearance','completed'),
                ('clearance_pending_amount','prepayment_clearance','reserved'),
                ('returned_amount','prepayment_return','completed'),('return_pending_amount','prepayment_return','reserved')]}

    async def list_payee_accounts(self, who: FinancePrincipal, entity: str, document_kind: str,
                                  document_id: str, cursor: str | None = None,
                                  limit: int = 50) -> dict[str, Any]:
        """资金账户按原场景和固定授权精确分流，普通报销应付仍走原实现。"""
        if document_kind!='funding_case':
            return await super().list_payee_accounts(who,entity,document_kind,document_id,cursor,limit)
        scope=request_digest([who.tenant_id,who.user_id,entity,'payees',document_kind,document_id])
        after=decode_cursor(cursor,scope,limit)
        async with self.pool.acquire() as conn,conn.transaction():
            doc=await self._funding_row(conn,who,document_id)
            if doc['legal_entity_id']!=entity:
                raise FinanceError('RESOURCE_NOT_FOUND',404,'专用资金单据不存在')
            content=decode(doc['content'])
            if content['scenario'] in CLOSING:
                return page([],0,None)
            accounts=await self._funding_visible_accounts(conn,who,entity,doc['owner_id'])
            visible=self._funding_case_accounts(content,doc,accounts)
            remaining=[a for a in visible if after is None or [a['ref']['source'],a['ref']['id']]>after]
            selected=remaining[:limit]
            return page([{key:a[key] for key in ('ref','account_name','masked_account','verified')} for a in selected],len(visible),
                encode_cursor(scope,[selected[-1]['ref']['source'],selected[-1]['ref']['id']]) if len(remaining)>limit else None)

    def _funding_case_accounts(self, content: dict[str, Any], doc: Any,
                               accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """预付保持原供应商，垫付和委托只允许原声明绑定的精确账户版本。"""
        if content['scenario']=='prepayment':
            return [a for a in accounts if a['ref']['kind']=='supplier_account' and a['subject_id']==content['supplier_ref']['id']]
        bound=content['authority']['account_ref']
        return [a for a in accounts if a['ref']==bound and
                (content['scenario']!='employee_advance' or a['subject_id']==doc['owner_id'])]

    async def _funding_current_assignment(self,conn: Connection,who: FinancePrincipal,doc: Any) -> bool:
        """资金历史指派只有匹配当前有效岗位代数才提供读取资格，撤权恢复不复活。"""
        return bool(await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('approver','reviewer')) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.document_id=$3 AND s.approver_user_id=$4)",who.tenant_id,doc['legal_entity_id'],doc['id'],who.user_id))

    def _legacy_assignment_visibility_sql(self) -> str:
        """资金关联步骤读取必须匹配当前代数，普通F0/F1/F2关联保持原语义。"""
        return " AND (NOT EXISTS(SELECT 1 FROM finance_expense_claim c WHERE c.tenant_id=w.tenant_id AND c.legal_entity_id=w.legal_entity_id AND c.id=w.document_id AND c.document_kind='funding_case') OR EXISTS(SELECT 1 FROM finance_grant_epoch e WHERE e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('approver','reviewer')))"
