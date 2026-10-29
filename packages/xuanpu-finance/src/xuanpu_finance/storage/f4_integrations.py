"""由真实配置投影集成状态，不把配置存在等同渠道健康或正式资格。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


async def configured_integrations(conn: Connection, tenant: str, entity: str) -> list[dict[str, Any]]:
    """只公开能力标识和配置阶段，不返回端点、密钥或完整配置。"""
    statuses = {kind: 'unconfigured' for kind in ('erp', 'payment', 'tax', 'archive', 'approval')}
    settlement = await conn.fetchrow('SELECT p.enabled,r.data FROM finance_settlement_policy p JOIN finance_settlement_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2', tenant, entity)
    compliance = await conn.fetchrow('SELECT p.enabled,r.data FROM finance_compliance_policy p JOIN finance_compliance_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2', tenant, entity)
    for row, table, mapping in [(settlement, 'finance_settlement_provider', {'payment': 'payment', 'erp': 'posting'}), (compliance, 'finance_compliance_provider', {'tax': 'tax', 'archive': 'archive'})]:
        if row is None:
            continue
        data = decode(row['data'])
        for kind, prefix in mapping.items():
            enabled = row['enabled'] and data.get(prefix+'_enabled', True)
            provider, version = data.get(prefix+'_provider'), data.get(prefix+'_configuration_version')
            if not provider or version is None:
                continue
            channel = await conn.fetchrow(f'SELECT enabled,data FROM {table} WHERE tenant_id=$1 AND legal_entity_id=$2 AND provider=$3 AND configuration_version=$4', tenant, entity, provider, version)
            if not enabled or (channel is not None and not channel['enabled']):
                statuses[kind] = 'disabled'
            elif channel is not None:
                statuses[kind] = 'configured_simulation' if decode(channel['data'])['simulation'] else 'formal_unverified'
    internal = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM finance_expense_policy WHERE tenant_id=$1 AND legal_entity_id=$2 AND enabled)', tenant, entity)
    if internal or (settlement is not None and settlement['enabled']) or (compliance is not None and compliance['enabled']):
        statuses['approval'] = 'configured_internal'
    return [{'id': f'{entity}:{kind}', 'kind': kind, 'status': status, 'capabilities': ['internal_approval'] if status == 'configured_internal' else ['query', 'simulation'] if status == 'configured_simulation' else []} for kind, status in statuses.items()]
