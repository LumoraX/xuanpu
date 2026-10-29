"""资金制度的显式注入、不可变版本及停用代数墓碑。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f4_notices import NotificationStore
from xuanpu_finance.storage.f4_overview import OverviewStore
from xuanpu_finance.storage.worker import WorkerStore

if TYPE_CHECKING:
    from xuanpu_finance.funding.models import FundingPolicy
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class FundingConfigStore(OverviewStore, NotificationStore, WorkerStore):
    """保留全部F0至F4组合顺序，仅在专用资金路径增加制度。"""

    async def configure_funding(self, policies: list[FundingPolicy]) -> None:
        """替换当前制度指针，不删除旧版本，禁用恢复增加代数。"""
        scopes = [(p.tenant_id, p.legal_entity_id) for p in policies]
        if len(scopes) != len(set(scopes)):
            raise FinanceError('CONFIGURATION_INVALID', 422, '资金制度公司重复')
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(617472008)')
            for policy in policies:
                await self._configure_funding_policy(conn, policy)
            for row in await conn.fetch('SELECT * FROM finance_funding_policy WHERE enabled FOR UPDATE'):
                if (row['tenant_id'], row['legal_entity_id']) not in scopes:
                    await conn.execute('UPDATE finance_funding_policy SET enabled=false,generation=generation+1 WHERE tenant_id=$1 AND legal_entity_id=$2', row['tenant_id'], row['legal_entity_id'])

    async def _configure_funding_policy(self, conn: Connection, policy: FundingPolicy) -> None:
        """制度正文去除启停位后不可变，并核对当前公司审批岗位。"""
        if policy.enabled:
            await self._policy(conn, policy.tenant_id, policy.legal_entity_id)
            for user in {s.approver_user_id for chain in policy.steps.values() for s in chain}:
                role = await conn.fetchval('SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE', policy.tenant_id, policy.legal_entity_id, user)
                if role not in {'approver', 'reviewer'}:
                    raise FinanceError('CONFIGURATION_INVALID', 422, '资金审批人缺少当前公司岗位')
        data = policy.model_dump(mode='json', exclude={'enabled'})
        digest = request_digest(data)
        old = await conn.fetchval('SELECT digest FROM finance_funding_policy_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND version=$3', policy.tenant_id, policy.legal_entity_id, policy.version)
        if old is not None and old != digest:
            raise FinanceError('POLICY_VERSION_CONFLICT', 409, '资金制度同版本正文不可覆盖')
        await conn.execute('INSERT INTO finance_funding_policy_revision VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING', policy.tenant_id, policy.legal_entity_id, policy.version, digest, json.dumps(data))
        await conn.execute('INSERT INTO finance_funding_policy(tenant_id,legal_entity_id,version,enabled) VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,legal_entity_id) DO UPDATE SET version=EXCLUDED.version,enabled=EXCLUDED.enabled,generation=CASE WHEN finance_funding_policy.version<>EXCLUDED.version OR finance_funding_policy.enabled<>EXCLUDED.enabled THEN finance_funding_policy.generation+1 ELSE finance_funding_policy.generation END', policy.tenant_id, policy.legal_entity_id, policy.version, policy.enabled)

    async def _funding_policy(self, conn: Connection, tenant: str, entity: str, *, require: bool = True) -> dict[str, Any] | None:
        """持有当前制度共享锁，避免实际执行期间被并发撤销。"""
        row = await conn.fetchrow('SELECT p.*,r.digest,r.data FROM finance_funding_policy p JOIN finance_funding_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2 FOR SHARE OF p', tenant, entity)
        if row is None or not row['enabled']:
            if require:
                raise FinanceError('FUNDING_POLICY_REQUIRED', 409, '公司未显式启用专用资金制度')
            return None
        return {**decode(row['data']), 'digest': row['digest'], 'generation': row['generation']}

    async def get_funding_options(self, who: FinancePrincipal, entity: str) -> dict[str, Any]:
        """人工辅助页取得显式场景和实际审批链，不自动推断制度。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await self._role(conn, who, entity)
            policy = await self._funding_policy(conn, who.tenant_id, entity, require=False)
            return {'legal_entity_id': entity, 'enabled': policy is not None,
                    'policy_version': policy['version'] if policy else None,
                    'scenarios': [{'scenario': s, 'steps': steps} for s, steps in policy['steps'].items()] if policy else [],
                    'simulation': await self._simulation(conn),
                    'accounts': [self._funding_account_view(a) for a in await self._funding_visible_accounts(conn,who,entity)]}

    async def _funding_visible_accounts(self, conn: Connection, who: FinancePrincipal, entity: str,
                                        employee_owner: str | None = None) -> list[dict[str, Any]]:
        """SQL只取当前可见且已核实账户，员工账户还必须匹配明确实际员工。"""
        rows=await conn.fetch("SELECT r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.enabled AND c.kind IN ('employee_account','supplier_account') AND r.data->'allowed_user_ids' ? $3 AND r.data->>'verified'='true' AND (c.kind='supplier_account' OR r.data->>'subject_id'=$4) ORDER BY c.kind,c.source,c.id",who.tenant_id,entity,who.user_id,employee_owner or who.user_id)
        return [decode(row['data']) for row in rows]

    def _funding_account_view(self, account: dict[str, Any]) -> dict[str, Any]:
        """建单前只展示受控引用及脱敏主体，不暴露渠道账户令牌或凭据。"""
        return {key:account[key] for key in ('ref','account_name','masked_account','verified','subject_id')}
