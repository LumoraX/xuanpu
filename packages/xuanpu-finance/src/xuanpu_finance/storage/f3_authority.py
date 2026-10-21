"""按公司、提供方和不可复活配置epoch维护税务权威版本。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


async def authority_position(conn: Connection, operation: Any, invoice_id: str, revision: int) -> tuple[bool, int]:
    """当前受控源可启动自己的版本序列，旧源在途只保全历史。"""
    tenant, entity = operation["tenant_id"], operation["legal_entity_id"]
    plan = decode(await conn.fetchval("SELECT data FROM finance_compliance_plan WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", tenant, entity, operation["plan_id"]))
    generation = int(plan["bindings"]["provider"]["generation"])
    current = await conn.fetchrow("SELECT p.enabled,r.data,v.generation AS provider_generation,v.enabled AS provider_enabled FROM finance_compliance_policy p JOIN finance_compliance_policy_revision r USING(tenant_id,legal_entity_id,version) JOIN finance_compliance_provider v ON(v.tenant_id=p.tenant_id AND v.legal_entity_id=p.legal_entity_id AND v.provider=$3 AND v.configuration_version=$4) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2", tenant, entity, operation["provider"], operation["configuration_version"])
    if current is None or not current["enabled"] or not current["provider_enabled"] or current["provider_generation"] != generation:
        return False, generation
    policy = decode(current["data"])
    if not policy["tax_enabled"] or (policy["tax_provider"], policy["tax_configuration_version"]) != (operation["provider"], operation["configuration_version"]):
        return False, generation
    scope = (tenant, entity, invoice_id, operation["provider"], operation["configuration_version"], generation)
    previous = await conn.fetchval("SELECT authority_revision FROM finance_invoice_authority_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND provider=$4 AND configuration_version=$5 AND provider_generation=$6", *scope)
    if previous is not None and revision <= previous:
        return False, generation
    await conn.execute("INSERT INTO finance_invoice_authority_revision VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(tenant_id,legal_entity_id,invoice_id,provider,configuration_version,provider_generation) DO UPDATE SET authority_revision=EXCLUDED.authority_revision", *scope, revision)
    return True, generation


def same_authority(state: dict[str, Any], operation: Any, generation: int) -> bool:
    """不同配置epoch的查验通过状态不能互相延长或回退。"""
    return (state.get("provider"), state.get("configuration_version"), state.get("provider_generation")) == (operation["provider"], operation["configuration_version"], generation)
