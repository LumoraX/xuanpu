"""F6同公司reviewer只读检查，从当前持久配置计算准备情况。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.readiness.checks import build_report
from xuanpu_finance.storage.f5_settlement import FundingStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.readiness.models import ReadinessReport
    from xuanpu_finance.storage.business_base import Connection

POLICY_QUERIES = {
    "expense": "SELECT p.version,p.enabled,p.generation,r.digest,r.data FROM finance_expense_policy p JOIN finance_expense_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2",
    "settlement": "SELECT p.version,p.enabled,p.generation,r.digest,r.data FROM finance_settlement_policy p JOIN finance_settlement_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2",
    "compliance": "SELECT p.version,p.enabled,p.generation,r.digest,r.data FROM finance_compliance_policy p JOIN finance_compliance_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2",
    "funding": "SELECT p.version,p.enabled,p.generation,r.digest,r.data FROM finance_funding_policy p JOIN finance_funding_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2",
}


def revision_object(raw: Any) -> dict[str, Any]:
    """合法JSONB非对象保留原值供摘要及模型拒绝，查询或解析故障仍上抛。"""
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else {"invalid_revision": value}


class ReadinessStore(FundingStore):
    """保持现有业务组合，只增加无写入、无执行许可的快照读取。"""

    async def get_finance_readiness(self, who: FinancePrincipal, entity: str) -> ReadinessReport:
        """同一只读可重复读事务核对当前公司reviewer及全部配置。"""
        async with (
            self.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            # 不能调用带FOR SHARE的旧授权辅助函数：只读事务不允许行锁。
            role = await conn.fetchval(
                "SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3",
                who.tenant_id,
                entity,
                who.user_id,
            )
            if role != "reviewer":
                raise FinanceError(
                    "READINESS_FORBIDDEN", 403, "仅当前公司财务reviewer可查看接入准备"
                )
            snapshot = await self._readiness_snapshot(conn, who.tenant_id, entity)
            return build_report(snapshot)

    async def _readiness_snapshot(
        self, conn: Connection, tenant: str, entity: str
    ) -> dict[str, Any]:
        """每条业务查询明确限定租户和公司，原始正文只在事务内计算。"""
        company = await conn.fetchrow(
            "SELECT name,tax_id FROM finance_company WHERE tenant_id=$1 AND legal_entity_id=$2",
            tenant,
            entity,
        )
        roles = await conn.fetch(
            "SELECT g.user_id,g.role,e.role AS epoch_role,e.generation FROM finance_grant g LEFT JOIN finance_grant_epoch e USING(tenant_id,legal_entity_id,user_id) WHERE g.tenant_id=$1 AND g.legal_entity_id=$2 ORDER BY g.user_id",
            tenant,
            entity,
        )
        epochs = await conn.fetch(
            "SELECT user_id,role,generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 ORDER BY user_id",
            tenant,
            entity,
        )
        policies = {}
        for kind, query in POLICY_QUERIES.items():
            row = await conn.fetchrow(query, tenant, entity)
            if row is not None:
                policies[kind] = {
                    **revision_object(row["data"]),
                    **{k: row[k] for k in ("version", "enabled", "generation", "digest")},
                }
        providers = await self._readiness_providers(conn, tenant, entity)
        catalog = await conn.fetch(
            "SELECT c.kind,c.source,c.id,c.version,c.enabled,c.generation,r.digest,r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.tenant_id=$1 AND c.legal_entity_id=$2 ORDER BY c.kind,c.source,c.id",
            tenant,
            entity,
        )
        mode = await conn.fetchval(
            "SELECT m.simulation FROM finance_settlement_mode m JOIN finance_company c ON c.tenant_id=$1 AND c.legal_entity_id=$2 WHERE m.singleton",
            tenant,
            entity,
        )
        compliance_mode = await conn.fetchval(
            "SELECT m.simulation FROM finance_compliance_mode m JOIN finance_company c ON c.tenant_id=$1 AND c.legal_entity_id=$2 WHERE m.singleton",
            tenant,
            entity,
        )
        return {
            "scope": {"tenant_id": tenant, "legal_entity_id": entity},
            "company": dict(company) if company else {},
            "roles": [dict(r) for r in roles],
            "epochs": [dict(r) for r in epochs],
            "policies": policies,
            "providers": providers,
            "catalog": [
                {
                    **revision_object(r["data"]),
                    **{
                        k: r[k]
                        for k in (
                            "kind",
                            "source",
                            "id",
                            "version",
                            "enabled",
                            "generation",
                            "digest",
                        )
                    },
                }
                for r in catalog
            ],
            "simulation": mode is True and compliance_mode is True,
            "settlement_simulation": mode,
            "compliance_simulation": compliance_mode,
        }

    async def _readiness_providers(
        self, conn: Connection, tenant: str, entity: str
    ) -> list[dict[str, Any]]:
        """保留渠道固定绑定、启停及代数供摘要，永不直接返回URL。"""
        settlement = await conn.fetch(
            "SELECT provider,configuration_version,enabled,generation,digest,data FROM finance_settlement_provider WHERE tenant_id=$1 AND legal_entity_id=$2 ORDER BY provider,configuration_version",
            tenant,
            entity,
        )
        compliance = await conn.fetch(
            "SELECT provider,configuration_version,enabled,generation,digest,data FROM finance_compliance_provider WHERE tenant_id=$1 AND legal_entity_id=$2 ORDER BY provider,configuration_version",
            tenant,
            entity,
        )
        return [
            {
                **revision_object(r["data"]),
                **{
                    k: r[k]
                    for k in (
                        "provider",
                        "configuration_version",
                        "enabled",
                        "generation",
                        "digest",
                    )
                },
            }
            for r in [*settlement, *compliance]
        ]
