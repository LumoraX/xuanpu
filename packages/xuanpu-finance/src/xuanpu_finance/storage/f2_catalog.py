"""F2不可变配置、环境标记和按权限过滤的受控目录。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_exceptions import ExpenseStore
from xuanpu_finance.storage.common import decode, decode_cursor, encode_cursor, page

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import BusinessRef
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy
    from xuanpu_finance.storage.business_base import Connection


class SettlementCatalogStore(ExpenseStore):
    """运行时只注入无凭据配置，业务写操作不能创建已核实主数据。"""

    async def configure_settlements(
        self,
        policies: list[SettlementPolicy],
        business_documents: list[BusinessSnapshot],
        payee_accounts: list[PayeeAccount],
        providers: list[dict[str, Any]],
        *,
        simulation: bool = False,
    ) -> None:
        """同事务替换当前配置，版本内容不可变且环境持久不可切换。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(617472005)")
            marker = await conn.fetchval(
                "SELECT simulation FROM finance_settlement_mode WHERE singleton FOR UPDATE"
            )
            if marker is not None and marker != simulation:
                raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "独立财务库环境不能切换")
            if simulation and not str(await conn.fetchval("SELECT current_database()")).startswith(
                "finance_test"
            ):
                raise FinanceError(
                    "SIMULATION_DATABASE_REQUIRED", 422, "模拟仅允许独立finance_test数据库"
                )
            await conn.execute(
                "INSERT INTO finance_settlement_mode VALUES(true,$1) ON CONFLICT DO NOTHING",
                simulation,
            )
            await self._configure_providers(conn, providers, simulation)
            await self._configure_catalog(conn, [*business_documents, *payee_accounts])
            scopes = [(p.tenant_id, p.legal_entity_id) for p in policies]
            if len(set(scopes)) != len(scopes):
                raise FinanceError("CONFIGURATION_INVALID", 422, "结算制度公司重复")
            for policy in policies:
                if not simulation and not policy.require_verified_invoice:
                    raise FinanceError("VERIFICATION_REQUIRED", 422, "正式执行不能关闭有效查验要求")
                await self._configure_settlement_policy(conn, policy)
            rows = await conn.fetch(
                "SELECT * FROM finance_settlement_policy WHERE enabled FOR UPDATE"
            )
            for row in rows:
                if (row["tenant_id"], row["legal_entity_id"]) not in scopes:
                    await conn.execute(
                        "UPDATE finance_settlement_policy SET enabled=false,generation=generation+1 WHERE tenant_id=$1 AND legal_entity_id=$2",
                        row["tenant_id"],
                        row["legal_entity_id"],
                    )
            await conn.execute(
                "UPDATE finance_execution_grant g SET active=false WHERE plan_id IS NOT NULL AND active AND NOT EXISTS(SELECT 1 FROM finance_settlement_policy p JOIN finance_settlement_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=g.tenant_id AND p.legal_entity_id=g.legal_entity_id AND p.enabled AND p.version=g.policy_version AND p.generation=g.policy_generation AND r.digest=g.policy_digest)"
            )

    async def _configure_providers(
        self, conn: Connection, providers: list[dict[str, Any]], simulation: bool
    ) -> None:
        """目标地址/协议能力同版本不可换，停用后恢复保留代数墓碑。"""
        keys = [(p["provider"], p["configuration_version"]) for p in providers]
        if len(set(keys)) != len(keys):
            raise FinanceError("CONFIGURATION_INVALID", 422, "渠道版本重复")
        for data in providers:
            if data["simulation"] != simulation:
                raise FinanceError("SIMULATION_MODE_CONFLICT", 422, "渠道与数据库环境不一致")
            body = {k: v for k, v in data.items() if k != "enabled"}
            digest = request_digest(body)
            old = await conn.fetchrow(
                "SELECT * FROM finance_settlement_provider WHERE provider=$1 AND configuration_version=$2 FOR UPDATE",
                data["provider"],
                data["configuration_version"],
            )
            if old is not None and old["digest"] != digest:
                raise FinanceError("PROVIDER_VERSION_CONFLICT", 409, "渠道同配置版本内容不可更换")
            await conn.execute(
                "INSERT INTO finance_settlement_provider(provider,configuration_version,tenant_id,legal_entity_id,digest,data,enabled) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7) ON CONFLICT(provider,configuration_version) DO UPDATE SET enabled=EXCLUDED.enabled,generation=CASE WHEN finance_settlement_provider.enabled<>EXCLUDED.enabled THEN finance_settlement_provider.generation+1 ELSE finance_settlement_provider.generation END",
                data["provider"],
                data["configuration_version"],
                data["tenant_id"],
                data["legal_entity_id"],
                digest,
                json.dumps(body),
                data["enabled"],
            )
        for old in await conn.fetch("SELECT * FROM finance_settlement_provider FOR UPDATE"):
            if (old["provider"], old["configuration_version"]) in keys:
                continue
            active = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM finance_settlement_operation WHERE provider=$1 AND configuration_version=$2 AND status IN ('accepted','running','dispatching','awaiting_provider','outcome_unknown'))",
                old["provider"],
                old["configuration_version"],
            )
            if active:
                raise FinanceError("PROVIDER_INFLIGHT", 409, "在途渠道必要配置不能移除")
            await conn.execute(
                "UPDATE finance_settlement_provider SET enabled=false,generation=generation+1 WHERE provider=$1 AND configuration_version=$2 AND enabled",
                old["provider"],
                old["configuration_version"],
            )

    async def _configure_catalog(self, conn: Connection, entries: list[Any]) -> None:
        """目录快照逐版本保留，切回旧版本也不会复活旧计划。"""
        keys: list[tuple[str, ...]] = []
        for entry in entries:
            data = entry.model_dump(mode="json", exclude_none=True)
            ref = data["ref"]
            key = (
                data["tenant_id"],
                data["legal_entity_id"],
                ref["kind"],
                ref["source"],
                ref["id"],
            )
            if key in keys:
                raise FinanceError("CONFIGURATION_INVALID", 422, "同一目录标识重复")
            keys.append(key)
            old = await conn.fetchrow(
                "SELECT digest FROM finance_settlement_catalog_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND kind=$3 AND source=$4 AND id=$5 AND version=$6",
                *key,
                ref["version"],
            )
            digest = request_digest(data)
            if old is not None and old["digest"] != digest:
                raise FinanceError("CATALOG_VERSION_CONFLICT", 409, "目录同来源版本内容不可覆盖")
            await conn.execute(
                "INSERT INTO finance_settlement_catalog_revision VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT DO NOTHING",
                *key,
                ref["version"],
                digest,
                json.dumps(data),
            )
            await conn.execute(
                "INSERT INTO finance_settlement_catalog(tenant_id,legal_entity_id,kind,source,id,version) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(tenant_id,legal_entity_id,kind,source,id) DO UPDATE SET version=EXCLUDED.version,enabled=true,generation=CASE WHEN finance_settlement_catalog.version<>EXCLUDED.version OR NOT finance_settlement_catalog.enabled THEN finance_settlement_catalog.generation+1 ELSE finance_settlement_catalog.generation END",
                *key,
                ref["version"],
            )
        for old in await conn.fetch(
            "SELECT * FROM finance_settlement_catalog WHERE enabled FOR UPDATE"
        ):
            key = tuple(
                old[name] for name in ("tenant_id", "legal_entity_id", "kind", "source", "id")
            )
            if key not in keys:
                await conn.execute(
                    "UPDATE finance_settlement_catalog SET enabled=false,generation=generation+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND kind=$3 AND source=$4 AND id=$5",
                    *key,
                )

    async def _configure_settlement_policy(
        self, conn: Connection, policy: SettlementPolicy
    ) -> None:
        """验证真实公司岗位，再保存不可变策略正文和当前指针。"""
        data = policy.model_dump(mode="json", exclude={"enabled"})
        for user in set(policy.payment_executor_user_ids + policy.posting_executor_user_ids):
            role = await conn.fetchval(
                "SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE",
                policy.tenant_id,
                policy.legal_entity_id,
                user,
            )
            if policy.enabled and role != "reviewer":
                raise FinanceError("CONFIGURATION_INVALID", 422, "结算经办必须当前公司reviewer")
        for step in [*policy.payment_steps, *policy.posting_steps, *policy.reversal_steps]:
            role = await conn.fetchval(
                "SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE",
                policy.tenant_id,
                policy.legal_entity_id,
                step.approver_user_id,
            )
            if policy.enabled and role not in {"approver", "reviewer"}:
                raise FinanceError("CONFIGURATION_INVALID", 422, "专用审批人缺少当前公司资格")
        for prefix in ("payment", "posting"):
            row = await conn.fetchrow(
                "SELECT * FROM finance_settlement_provider WHERE tenant_id=$1 AND legal_entity_id=$2 AND provider=$3 AND configuration_version=$4",
                policy.tenant_id,
                policy.legal_entity_id,
                data[prefix + "_provider"],
                data[prefix + "_configuration_version"],
            )
            if policy.enabled and row is None:
                raise FinanceError("CONFIGURATION_INVALID", 422, "制度引用未配置渠道版本")
        digest = request_digest(data)
        old = await conn.fetchval(
            "SELECT digest FROM finance_settlement_policy_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND version=$3",
            policy.tenant_id,
            policy.legal_entity_id,
            policy.version,
        )
        if old is not None and old != digest:
            raise FinanceError("POLICY_VERSION_CONFLICT", 409, "结算策略同版本不可更改")
        await conn.execute(
            "INSERT INTO finance_settlement_policy_revision VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING",
            policy.tenant_id,
            policy.legal_entity_id,
            policy.version,
            digest,
            json.dumps(data),
        )
        await conn.execute(
            "INSERT INTO finance_settlement_policy(tenant_id,legal_entity_id,version,enabled) VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,legal_entity_id) DO UPDATE SET version=EXCLUDED.version,enabled=EXCLUDED.enabled,generation=CASE WHEN finance_settlement_policy.version<>EXCLUDED.version OR finance_settlement_policy.enabled<>EXCLUDED.enabled THEN finance_settlement_policy.generation+1 ELSE finance_settlement_policy.generation END",
            policy.tenant_id,
            policy.legal_entity_id,
            policy.version,
            policy.enabled,
        )

    async def _settlement_policy(
        self, conn: Connection, tenant: str, entity: str, *, require: bool = True
    ) -> dict[str, Any] | None:
        """当前指针带代数与摘要，执行期间锁住以防并发撤销。"""
        row = await conn.fetchrow(
            "SELECT p.*,r.digest,r.data FROM finance_settlement_policy p JOIN finance_settlement_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2 FOR SHARE OF p",
            tenant,
            entity,
        )
        if row is None or not row["enabled"]:
            if require:
                raise FinanceError("SETTLEMENT_POLICY_REQUIRED", 409, "公司未启用结算策略")
            return None
        return {**decode(row["data"]), "digest": row["digest"], "generation": row["generation"]}

    async def _simulation(self, conn: Connection) -> bool:
        """实例重建也从独立库持久标记读取模式。"""
        return bool(
            await conn.fetchval("SELECT simulation FROM finance_settlement_mode WHERE singleton")
        )

    async def _catalog_ref(
        self,
        conn: Connection,
        who: FinancePrincipal,
        entity: str,
        ref: BusinessRef,
        *,
        enforce_scope: bool = True,
    ) -> dict[str, Any]:
        """精确匹配来源/版本/摘要及有效代数，不能引用过时快照。"""
        row = await conn.fetchrow(
            "SELECT c.*,r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.kind=$3 AND c.source=$4 AND c.id=$5 FOR SHARE OF c",
            who.tenant_id,
            entity,
            ref.kind,
            ref.source,
            ref.id,
        )
        if row is None or not row["enabled"]:
            raise FinanceError("CATALOG_REFERENCE_INVALID", 409, "受控目录不存在或已停用")
        data = decode(row["data"])
        if data["ref"] != ref.model_dump() or (
            enforce_scope and who.user_id not in data["allowed_user_ids"]
        ):
            raise FinanceError("CATALOG_REFERENCE_INVALID", 409, "受控来源版本摘要或员工范围不匹配")
        return {**data, "generation": row["generation"]}

    async def list_business_documents(
        self,
        who: FinancePrincipal,
        entity: str,
        kind: str,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """只返回员工实际范围内的已同步或原生业务快照。"""
        if kind not in {"supplier", "contract", "order", "acceptance"}:
            raise FinanceError("CATALOG_KIND_INVALID", 422, "业务目录类型无效")
        return await self._settlement_catalog_page(who, entity, kind, query, cursor, limit)

    async def _settlement_catalog_page(
        self,
        who: FinancePrincipal,
        entity: str,
        kind: str,
        query: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """过滤后独立计数并按来源和标识稳定分页。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, kind, query])
        after = decode_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction():
            await self._role(conn, who, entity)
            rows = await conn.fetch(
                "SELECT r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.kind=$3 AND c.enabled ORDER BY c.source,c.id",
                who.tenant_id,
                entity,
                kind,
            )
            entries = [decode(r["data"]) for r in rows]
            entries = [
                d
                for d in entries
                if who.user_id in d["allowed_user_ids"]
                and (query is None or query.casefold() in d["name"].casefold())
            ]
            remaining = [
                d for d in entries if after is None or [d["ref"]["source"], d["ref"]["id"]] > after
            ]
            visible = remaining[:limit]
            fields = {
                "ref",
                "name",
                "verification_state",
                "amount",
                "party_tax_id",
                "counterparty_tax_id",
                "occurred_on",
            }
            return page(
                [{k: v for k, v in d.items() if k in fields} for d in visible],
                len(entries),
                encode_cursor(scope, [visible[-1]["ref"]["source"], visible[-1]["ref"]["id"]])
                if len(remaining) > limit
                else None,
            )

    async def get_settlement_options(self, who: FinancePrincipal, entity: str) -> dict[str, Any]:
        """人工页面只取实际开放期间和本人受控经办资格。"""
        async with self.pool.acquire() as conn, conn.transaction():
            role = await self._role(conn, who, entity)
            policy = await self._settlement_policy(conn, who.tenant_id, entity, require=False)
            payment = (
                policy is not None
                and role == "reviewer"
                and who.user_id in policy["payment_executor_user_ids"]
            )
            posting = (
                policy is not None
                and role == "reviewer"
                and who.user_id in policy["posting_executor_user_ids"]
            )
            return {
                "legal_entity_id": entity,
                "simulation": await self._simulation(conn),
                "open_periods": policy["open_periods"] if policy else [],
                "ledger_id": policy["ledger_id"] if policy else None,
                "payment_allowed": payment,
                "posting_allowed": posting,
                "reversal_allowed": posting,
            }
