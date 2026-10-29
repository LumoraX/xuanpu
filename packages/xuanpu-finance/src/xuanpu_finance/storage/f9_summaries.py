"""内部报销有界摘要：同一快照授权、计数和倒序键集分页。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_base import money
from xuanpu_finance.storage.common import encode_cursor, page, timestamp_cursor
from xuanpu_finance.storage.f8_materials import MaterialStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal

STATUSES = frozenset({'draft', 'submitted', 'under_review', 'approved', 'rejected', 'withdrawn'})
SUMMARY_WHERE = """c.document_kind='expense_claim' AND c.tenant_id=$1 AND c.legal_entity_id=$2
 AND (c.owner_id=$3 OR $4::text='reviewer' OR ($4::text='approver' AND EXISTS(
 SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s
 ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id)
 JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id
 AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation
 AND e.role IN ('approver','reviewer'))
 WHERE w.tenant_id=c.tenant_id AND w.legal_entity_id=c.legal_entity_id
 AND w.document_id=c.id AND s.approver_user_id=$3)))
 AND ($5::text='visible' OR c.owner_id=$3) AND ($6::text IS NULL OR c.status=$6)
 AND ($7::text='' OR strpos(lower(c.id),lower($7))>0
 OR strpos(lower(c.content->>'reason'),lower($7))>0)"""
SUMMARY_COLUMNS = "c.id,c.legal_entity_id,c.owner_id,c.version,c.status,c.total,c.content->>'reason' AS reason,c.created_at,c.record_mode"


def validate_summary_query(view: str, status: str | None, q: str, limit: int) -> str:
    """领域入口同样约束长度与枚举，字面量搜索不依赖SQL通配符转义。"""
    if view not in {'mine', 'visible'} or (status is not None and status not in STATUSES):
        raise FinanceError('SUMMARY_FILTER_INVALID', 422, '摘要筛选无效')
    if not isinstance(q, str) or len(q) > 128 or not 1 <= limit <= 50:
        raise FinanceError('SUMMARY_FILTER_INVALID', 422, '摘要查询超出范围')
    return q.strip()


class SummaryStore(MaterialStore):
    """摘要从实际列投影，永不读取完整原文、附件或工作流快照。"""

    async def list_expense_claim_summaries(
        self, who: FinancePrincipal, entity: str, *, view: str = 'mine',
        status: str | None = None, q: str = '', cursor: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        """授权、独立总数和页面使用同一repeatable_read快照及当前岗位代数。"""
        q = validate_summary_query(view, status, q, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read'):
            role = await self._role(conn, who, entity)
            generation = await conn.fetchval(
                'SELECT generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3',
                who.tenant_id, entity, who.user_id)
            scope = request_digest([who.tenant_id, who.user_id, entity, view, status, q,
                                    'expense_summaries_desc', role, generation])
            stamp, last = timestamp_cursor(cursor, scope, limit)
            args = (who.tenant_id, entity, who.user_id, role, view, status, q)
            total = await conn.fetchval(
                'SELECT count(*) FROM finance_expense_claim c WHERE ' + SUMMARY_WHERE, *args)
            rows = await conn.fetch(
                'SELECT ' + SUMMARY_COLUMNS + ' FROM finance_expense_claim c WHERE '
                + SUMMARY_WHERE + ' AND ($8::timestamptz IS NULL OR (c.created_at,c.id)<($8,$9::text))'
                + ' ORDER BY c.created_at DESC,c.id DESC LIMIT $10', *args, stamp, last, limit + 1)
            selected = rows[:limit]
            next_cursor = (encode_cursor(scope, [selected[-1]['created_at'].isoformat(), selected[-1]['id']])
                           if len(rows) > limit else None)
            items = [{**dict(row), 'total': money(row['total']),
                      'created_at': row['created_at'].isoformat()} for row in selected]
            return page(items, total, next_cursor)
