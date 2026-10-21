"""实际原件责任、不可变授权声明及人工追加撤销。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f5_config import FundingConfigStore

if TYPE_CHECKING:
    from xuanpu_finance.funding.models import FundingDraft
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class FundingAuthorityStore(FundingConfigStore):
    """材料接收只是申报，只有实际人工决定能产生内部核对事实。"""

    async def _funding_evidence(self, conn: Connection, who: FinancePrincipal, entity: str,
                                ids: list[str], *, supporting: bool = False) -> list[dict[str, Any]]:
        """回执、收件项、操作、固定哈希和环境必须形成完整责任链。"""
        records = []
        simulation = await self._simulation(conn)
        for id in sorted(ids):
            row = await conn.fetchrow("SELECT r.*,i.state,i.receipt_id,i.object_key AS item_object,i.actual_sha256,i.content_type,i.size_bytes,i.file_ref AS item_file,o.source_simulation,to_jsonb(o)->>'purpose' AS purpose FROM finance_receipt r JOIN finance_item i ON(i.tenant_id=r.tenant_id AND i.legal_entity_id=r.legal_entity_id AND i.operation_id=r.operation_id AND i.position=r.position) JOIN finance_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id) WHERE r.tenant_id=$1 AND r.legal_entity_id=$2 AND r.id=$3 FOR SHARE OF r,i,o", who.tenant_id, entity, id)
            if row is None:
                raise FinanceError('FUNDING_EVIDENCE_INVALID', 409, '资金原件不存在或不属于当前公司')
            await self._authorize(conn, who, row)
            file_ref = decode(row['file_ref'])
            valid = (row['state'] in {'material_received', 'parsed', 'duplicate_linked'}
                     and row['source_simulation'] == simulation and row['receipt_id'] == id
                     and row['object_key'] and row['object_key'] == row['item_object']
                     and len(row['sha256']) == 64 and row['sha256'] == row['actual_sha256'] == file_ref.get('sha256')
                     and file_ref == decode(row['item_file']) and row['size_bytes'] and row['size_bytes'] > 0
                     and row['content_type'])
            if supporting:
                valid = valid and row['state'] == 'material_received' and row['purpose'] == 'supporting_material'
            if not valid:
                raise FinanceError('FUNDING_EVIDENCE_INVALID', 409, '原件未成功保全、类型/摘要/环境或责任索引不匹配')
            records.append({'id': id, 'sha256': row['sha256'], 'source_version': str(file_ref['revision']),
                            'object_key': row['object_key'], 'source_simulation': simulation,
                            'state': row['state'], 'verification_state': 'not_checked'})
        return records

    async def _funding_authority_data(self, conn: Connection, who: FinancePrincipal,
                                      draft: FundingDraft) -> dict[str, Any] | None:
        """按自然责任范围及实际原件摘要确定共享凭据身份。"""
        spec = draft.authority
        if spec is None:
            return None
        evidence = await self._funding_evidence(conn, who, draft.legal_entity_id, spec.evidence_ids, supporting=True)
        hashes = sorted({e['sha256'] for e in evidence})
        scope: dict[str, Any] = {'kind': spec.kind}
        if spec.kind == 'employee_payment':
            scope['owner_id'] = who.user_id
        elif spec.kind == 'return_evidence':
            scope['supplier_ref'] = {k:v for k,v in draft.supplier_ref.model_dump().items() if k in {'kind','source','id'}}
        else:
            scope['supplier_ref'] = {k:v for k,v in draft.supplier_ref.model_dump().items() if k in {'kind','source','id'}}
        if spec.kind == 'prepayment_terms':
            scope['contracts'] = sorted([r.model_dump() for r in draft.business_refs if r.kind == 'contract'], key=request_digest)
        else:
            scope['reference'] = spec.reference
        normalized = spec.model_dump(mode='json', exclude={'evidence_ids'})
        body = {'spec': normalized, 'scope': scope, 'supplier_ref': draft.supplier_ref.model_dump(), 'evidence_hashes': hashes,
                'business_refs': sorted([r.model_dump() for r in draft.business_refs], key=request_digest)}
        return {'kind': spec.kind, 'scope_digest': request_digest(scope), 'body_digest': request_digest(body),
                'evidence_digest': request_digest(hashes), 'hashes': hashes,
                'data': {**spec.model_dump(mode='json'), 'scope': scope}}

    async def _register_funding_authority(self, conn: Connection, who: FinancePrincipal,
                                          draft: FundingDraft) -> str | None:
        """自然范围和每个原件哈希串行，换编号不能分裂共享额度。"""
        data = await self._funding_authority_data(conn, who, draft)
        if data is None:
            return None
        locks = [data['scope_digest'], *data['hashes']]
        for value in sorted(locks):
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', request_digest([who.tenant_id, draft.legal_entity_id, data['kind'], value]))
        old = await conn.fetchrow('SELECT * FROM finance_funding_authority WHERE tenant_id=$1 AND legal_entity_id=$2 AND kind=$3 AND scope_digest=$4 FOR UPDATE', who.tenant_id, draft.legal_entity_id, data['kind'], data['scope_digest'])
        if old is not None:
            if old['body_digest'] != data['body_digest'] or old['evidence_digest'] != data['evidence_digest']:
                raise FinanceError('FUNDING_AUTHORITY_CONFLICT', 409, '相同原凭据责任范围正文或实际原件矛盾')
            if old['revoked']:
                raise FinanceError('FUNDING_AUTHORITY_REVOKED', 409, '原授权已撤销，不能重新申报复活')
            return str(old['id'])
        duplicate = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM finance_funding_original WHERE tenant_id=$1 AND legal_entity_id=$2 AND kind=$3 AND sha256=ANY($4::text[]))', who.tenant_id, draft.legal_entity_id, data['kind'], data['hashes'])
        if duplicate:
            raise FinanceError('FUNDING_AUTHORITY_CONFLICT', 409, '相同实际原件不能更换编号建立新责任')
        id = identifier()
        await conn.execute('INSERT INTO finance_funding_authority(tenant_id,legal_entity_id,id,kind,scope_digest,body_digest,evidence_digest,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)', who.tenant_id, draft.legal_entity_id, id, data['kind'], data['scope_digest'], data['body_digest'], data['evidence_digest'], json.dumps(data['data']))
        await conn.executemany('INSERT INTO finance_funding_original VALUES($1,$2,$3,$4,$5)', [(who.tenant_id, draft.legal_entity_id, data['kind'], sha, id) for sha in data['hashes']])
        return id

    async def _funding_authority(self, conn: Connection, doc: Any, *, lock: bool = False) -> Any:
        """通过同公司专用单据映射读取原授权并按稳定ID锁定。"""
        return await conn.fetchrow('SELECT a.* FROM finance_funding_authority a JOIN finance_funding_case c ON(c.tenant_id=a.tenant_id AND c.legal_entity_id=a.legal_entity_id AND c.authority_id=a.id) WHERE c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.document_id=$3' + (' FOR UPDATE OF a' if lock else ' FOR SHARE OF a'), doc['tenant_id'], doc['legal_entity_id'], doc['id'])

    async def _check_funding_authority(self, conn: Connection, doc: Any, *, lock: bool = False) -> Any:
        """任何新决定或发送都在线复核撤销、期限和原声明摘要。"""
        row = await self._funding_authority(conn, doc, lock=lock)
        if row is None:
            return None
        if row['revoked']:
            raise FinanceError('FUNDING_AUTHORITY_REVOKED', 409, '原授权已由实际人工撤销')
        data = decode(row['data'])
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if (data.get('valid_from') is not None and today < data['valid_from']) or (data.get('valid_until') is not None and today > data['valid_until']):
            raise FinanceError('FUNDING_AUTHORITY_EXPIRED', 409, '原授权尚未生效或已过期')
        return row

    def _funding_authority_view(self, row: Any) -> dict[str, Any] | None:
        """对外仅展示原申报及撤销状态，不把声明转为银行核实。"""
        if row is None:
            return None
        return {'id': row['id'], 'version': row['version'], 'revoked': row['revoked'],
                **{k: v for k, v in decode(row['data']).items() if k != 'scope'}}

    async def revoke_funding_authority(self, who: FinancePrincipal, id: str, expected_version: int,
                                       reason: str, key: str) -> dict[str, Any]:
        """真实当前reviewer通过CAS追加撤销，申请人不能自提权。"""
        if who.origin != 'human':
            raise FinanceError('HUMAN_REQUIRED', 403, '撤销授权必须由实际人工执行')
        if not reason.strip() or len(reason) > 2000 or expected_version < 1:
            raise FinanceError('FUNDING_REVOCATION_INVALID', 422, '撤销原因或版本无效')
        request = {'id': id, 'expected_version': expected_version, 'reason': reason}
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow('SELECT * FROM finance_funding_authority WHERE tenant_id=$1 AND id=$2 FOR UPDATE', who.tenant_id, id)
            if row is None:
                raise FinanceError('RESOURCE_NOT_FOUND', 404, '资金原授权不存在')
            previous = await self._business_previous(conn, who, row['legal_entity_id'], 'revokeFundingAuthority', key, request)
            if previous is not None:
                return previous
            owner = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM finance_funding_case f JOIN finance_expense_claim c ON(c.tenant_id=f.tenant_id AND c.legal_entity_id=f.legal_entity_id AND c.id=f.document_id) WHERE f.tenant_id=$1 AND f.legal_entity_id=$2 AND f.authority_id=$3 AND c.owner_id=$4)', who.tenant_id, row['legal_entity_id'], id, who.user_id)
            if await self._role(conn, who, row['legal_entity_id']) != 'reviewer' or owner:
                raise FinanceError('REVIEWER_REQUIRED', 403, '撤销须由与申请人分离的当前公司reviewer执行')
            if row['version'] != expected_version or row['revoked']:
                raise FinanceError('VERSION_CONFLICT', 409, '原授权版本或撤销状态已变化')
            row = await conn.fetchrow('UPDATE finance_funding_authority SET revoked=true,version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *', who.tenant_id, row['legal_entity_id'], id)
            assert row is not None
            await conn.execute('INSERT INTO finance_funding_authority_decision(tenant_id,legal_entity_id,id,authority_id,actor_id,expected_version,reason) VALUES($1,$2,$3,$4,$5,$6,$7)', who.tenant_id, row['legal_entity_id'], identifier(), id, who.user_id, expected_version, reason)
            docs = await conn.fetch('SELECT c.* FROM finance_funding_case f JOIN finance_expense_claim c ON(c.tenant_id=f.tenant_id AND c.legal_entity_id=f.legal_entity_id AND c.id=f.document_id) WHERE f.tenant_id=$1 AND f.legal_entity_id=$2 AND f.authority_id=$3 ORDER BY c.id', who.tenant_id, row['legal_entity_id'], id)
            for doc in docs:
                await self._event(conn, doc, 'funding.authority_revoked', {'authority_id': id, 'actor_id': who.user_id, 'reason': reason})
            result = self._funding_authority_view(row)
            assert result is not None
            return await self._business_remember(conn, who, row['legal_entity_id'], 'revokeFundingAuthority', key, request, result)
