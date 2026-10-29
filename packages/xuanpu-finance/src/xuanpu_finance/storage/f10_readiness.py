"""内部公司reviewer在单一只读可重复读快照中核对当前配置。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.readiness.internal_live import build_internal_report
from xuanpu_finance.storage.f9_summaries import SummaryStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection

# 直接读取当前指针，不通过revision JOIN推断外部启用状态。
EXTERNAL_CURRENT = {
    "settlement_policy": ("finance_settlement_policy", "version,enabled,generation", "version"),
    "compliance_policy": ("finance_compliance_policy", "version,enabled,generation", "version"),
    "funding_policy": ("finance_funding_policy", "version,enabled,generation", "version"),
    "settlement_provider": (
        "finance_settlement_provider",
        "provider,configuration_version,enabled,generation,digest",
        "provider,configuration_version",
    ),
    "compliance_provider": (
        "finance_compliance_provider",
        "provider,configuration_version,enabled,generation,digest",
        "provider,configuration_version",
    ),
    "catalog": (
        "finance_settlement_catalog",
        "kind,source,id,version,enabled,generation",
        "kind,source,id",
    ),
}


class InternalReadinessStore(SummaryStore):
    """复用F6配置快照，只增加内部profile安全核对投影。"""

    async def get_internal_records_readiness(
        self, who: FinancePrincipal, entity: str
    ) -> dict[str, Any]:
        """授权与所有配置共用readonly repeatable_read事务，任何查询故障正常上抛。"""
        async with (
            self.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            role = await conn.fetchval(
                "SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3",
                who.tenant_id,
                entity,
                who.user_id,
            )
            if role != "reviewer":
                raise FinanceError(
                    "READINESS_FORBIDDEN", 403, "仅当前公司财务reviewer可查看内部试用检查"
                )
            snapshot = await self._readiness_snapshot(conn, who.tenant_id, entity)
            snapshot["external_current"] = await self._internal_external_current(
                conn, who.tenant_id, entity
            )
            return build_internal_report(snapshot)

    async def _internal_external_current(
        self, conn: Connection, tenant: str, entity: str
    ) -> dict[str, Any]:
        """外部存在性直接读公司当前指针，孤立启用revision引用仍可被发现。"""
        current = {}
        for kind, (table, columns, ordering) in EXTERNAL_CURRENT.items():
            rows = await conn.fetch(
                f"SELECT {columns} FROM {table} WHERE tenant_id=$1 AND legal_entity_id=$2 ORDER BY {ordering}",
                tenant,
                entity,
            )
            current[kind] = [dict(row) for row in rows]
        return current
