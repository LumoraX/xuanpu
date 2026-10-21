"""按公司授权读取运营聚合，不展示敏感正文或改变业务结果。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import FinanceBase
from xuanpu_finance.storage.f4_notice_sources import VISIBLE_SQL

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class OverviewStore(FinanceBase):
    """仅当前reviewer可读公司运行指标，不继承平台管理员权限。"""

    async def get_finance_overview(self, who: FinancePrincipal, entity: str) -> dict[str, Any]:
        """公司内独立统计队列、待审及通知积压，total不来自当前页。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation='repeatable_read'):
            if await self._role(conn, who, entity) != 'reviewer':
                raise FinanceError('REVIEWER_REQUIRED', 403, '仅当前公司reviewer可查看运营概览')
            args = (who.tenant_id, entity)
            queues = {name: await queue_metrics(conn, table, *args) for name, table in [('imports', 'finance_operation'), ('settlement', 'finance_settlement_operation'), ('compliance', 'finance_compliance_operation')]}
            legacy = await conn.fetchrow("SELECT count(*) FILTER(WHERE status='under_review') AS pending,count(*) FILTER(WHERE status='under_review' AND due_at<now()) AS overdue FROM finance_workflow WHERE tenant_id=$1 AND legal_entity_id=$2", *args)
            f3 = await conn.fetchrow("SELECT count(*) FILTER(WHERE w.status='under_review') AS pending,count(*) FILTER(WHERE w.status='under_review' AND p.expires_at<now()) AS overdue FROM finance_compliance_workflow w JOIN finance_compliance_plan p ON(p.tenant_id=w.tenant_id AND p.legal_entity_id=w.legal_entity_id AND p.id=w.plan_id) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2", *args)
            current_notices = VISIBLE_SQL.replace(' AND n.recipient_id=$3', '')
            inbox = await conn.fetchrow('SELECT count(*) AS total,count(*) FILTER(WHERE n.read_at IS NULL) AS unread FROM finance_notification n WHERE '+current_notices, *args)
            outbox = await conn.fetchrow("SELECT count(*) FILTER(WHERE status='pending') AS pending,count(*) FILTER(WHERE inbox_diagnostic IS NOT NULL) AS diagnostic FROM finance_outbox WHERE tenant_id=$1 AND legal_entity_id=$2", *args)
            assert legacy is not None and f3 is not None and inbox is not None and outbox is not None
            return {'legal_entity_id': entity, 'generated_at': datetime.now(UTC).isoformat(), 'queues': queues, 'approvals': {'pending': legacy['pending']+f3['pending'], 'overdue': legacy['overdue']+f3['overdue']}, 'notifications': {**dict(inbox), 'outbox_pending': outbox['pending'], 'outbox_diagnostic': outbox['diagnostic'], 'external_channel': 'none', 'native_channel': 'finance_inbox'}}


async def queue_metrics(conn: Connection, table: str, tenant: str, entity: str) -> dict[str, Any]:
    """受控表内按持久状态汇总；不读取请求与回执正文。"""
    if table not in {'finance_operation', 'finance_settlement_operation', 'finance_compliance_operation'}:
        raise ValueError('非受控队列表')
    rows = await conn.fetch(f'SELECT status,count(*) AS count,min(created_at) AS oldest FROM {table} WHERE tenant_id=$1 AND legal_entity_id=$2 GROUP BY status', tenant, entity)
    active = {'accepted','running','dispatching','awaiting_provider','outcome_unknown'}
    oldest = min((r['oldest'] for r in rows if r['status'] in active), default=None)
    unknown = next((r['oldest'] for r in rows if r['status']=='outcome_unknown'), None)
    return {'status_counts': {r['status']: r['count'] for r in rows}, 'total': sum(r['count'] for r in rows), 'oldest_created_at': oldest.isoformat() if oldest else None, 'oldest_unknown_created_at': unknown.isoformat() if unknown else None}
