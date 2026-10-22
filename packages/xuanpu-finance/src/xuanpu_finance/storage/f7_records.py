"""普通报销的授权历史和指定版本材料关联，不赋予任何业务执行权限。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, encode_cursor, page, timestamp_cursor
from xuanpu_finance.storage.f6_readiness import ReadinessStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection

# 修订主键包含版本，避免同一单据的多个快照共用分页标识。
HISTORY = """SELECT 'revision:'||h.id||':'||h.version::text AS id,
 'revision'::text AS kind,h.created_at,h.data FROM finance_expense_history h
 WHERE h.tenant_id=$1 AND h.legal_entity_id=$2 AND h.id=$3
 UNION ALL SELECT 'decision:'||d.id AS id,'decision'::text AS kind,d.created_at,
 d.data||jsonb_build_object('actor_id',d.actor_id,'action',d.action,
 'workflow_id',d.workflow_id,'step_id',d.step_id) AS data
 FROM finance_decision d JOIN finance_workflow w
 ON(w.tenant_id=d.tenant_id AND w.legal_entity_id=d.legal_entity_id AND w.id=d.workflow_id)
 WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.document_id=$3"""


class RecordStore(ReadinessStore):
    """复用当前单据读权，并对分派人补核当前有效岗位代数。"""

    async def _expense_record_row(
        self, conn: Connection, who: FinancePrincipal, id: str
    ) -> Any:
        """每次读取先核当前租户、公司、类型及未撤销的实际分派。"""
        row = await self._claim_row(conn, who, id)
        if row['document_kind'] != 'expense_claim':
            raise FinanceError('RESOURCE_NOT_FOUND', 404, '报销不存在')
        role = await self._role(conn, who, row['legal_entity_id'])
        if row['owner_id'] != who.user_id and role != 'reviewer':
            if not await self._funding_current_assignment(conn, who, row):
                raise FinanceError('RESOURCE_NOT_FOUND', 404, '报销不存在')
        return row

    async def list_expense_claim_history(
        self, who: FinancePrincipal, id: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """合并不可变原快照与实际人工决定，按时间和带前缀标识稳定分页。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read'):
            doc = await self._expense_record_row(conn, who, id)
            entity = doc['legal_entity_id']
            scope = request_digest([who.tenant_id, who.user_id, entity, id, 'expense_history'])
            stamp, last = timestamp_cursor(cursor, scope, limit)
            args = (who.tenant_id, entity, id)
            total = await conn.fetchval('SELECT count(*) FROM (' + HISTORY + ') h', *args)
            rows = await conn.fetch(
                'SELECT * FROM (' + HISTORY + ') h WHERE ($4::timestamptz IS NULL OR '
                '(created_at,id)>($4,$5::text)) ORDER BY created_at,id LIMIT $6',
                *args, stamp, last, limit + 1,
            )
            visible = rows[:limit]
            next_cursor = encode_cursor(scope, [visible[-1]['created_at'].isoformat(), visible[-1]['id']]) if len(rows) > limit else None
            return page([{'id': r['id'], 'kind': r['kind'], 'created_at': r['created_at'].isoformat(), 'data': decode(r['data'])} for r in visible], total, next_cursor)

    async def _expense_material_snapshot(
        self, conn: Connection, doc: Any, version: int | None
    ) -> dict[str, Any]:
        """当前版本使用实时快照，显式历史版本只能来自持久不可变快照。"""
        if version is None:
            return await self._claim_snapshot(conn, doc)
        if version < 1:
            raise FinanceError('DOCUMENT_VERSION_INVALID', 422, '单据版本必须大于零')
        value = await conn.fetchval(
            'SELECT data FROM finance_expense_history WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND version=$4',
            doc['tenant_id'], doc['legal_entity_id'], doc['id'], version,
        )
        if value is None:
            raise FinanceError('RESOURCE_NOT_FOUND', 404, '报销版本不存在')
        return decode(value)

    async def _expense_material_linked(
        self, conn: Connection, doc: Any, snapshot: dict[str, Any], receipt: str
    ) -> bool:
        """必须既存在该快照的证据，又具有真实逐版本关联或该快照实际分配票据关联。"""
        if receipt not in {e['id'] for e in snapshot['evidence']}:
            return False
        return bool(await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM finance_expense_evidence e
             WHERE e.tenant_id=$1 AND e.legal_entity_id=$2 AND e.document_id=$3
             AND e.receipt_id=$4 AND e.document_version<=$5 AND $4=ANY($6::text[]))
             OR EXISTS(SELECT 1 FROM finance_allocation a JOIN finance_invoice_receipt l
             ON(l.tenant_id=a.tenant_id AND l.legal_entity_id=a.legal_entity_id AND l.invoice_id=a.invoice_id)
             WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.document_id=$3
             AND l.receipt_id=$4 AND a.id=ANY($7::text[]))""",
            doc['tenant_id'], doc['legal_entity_id'], doc['id'], receipt,
            snapshot['version'], snapshot['content'].get('evidence_ids', []), snapshot['allocation_ids'],
        ))
