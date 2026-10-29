"""F3策略、渠道和来源环境的显式配置及版本墓碑。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.compliance.models import ComplianceProviderDescriptor
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f2_inbox import SettlementStore

if TYPE_CHECKING:
    from xuanpu_finance.compliance.models import CompliancePolicy
    from xuanpu_finance.storage.business_base import Connection


class ComplianceCatalogStore(SettlementStore):
    """F3当前策略可以停用，历史版本和撤权代数永不重置。"""

    async def _simulation(self, conn: Connection) -> bool:
        """新业务来源读取同库受控标记，独立F3不依赖存在F2渠道。"""
        return bool(await conn.fetchval("SELECT COALESCE((SELECT simulation FROM finance_compliance_mode WHERE singleton),(SELECT simulation FROM finance_settlement_mode WHERE singleton),false)"))

    async def configure_compliance(
        self, policies: list[CompliancePolicy], providers: list[dict[str, Any]], *, simulation: bool = False
    ) -> None:
        """独立配置税务归档，环境只能在空白测试数据库首次标记。"""
        providers = [ComplianceProviderDescriptor.model_validate(p).model_dump(mode="json") for p in providers]
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(617472006)")
            await self._compliance_mode_config(conn, simulation)
            scopes = [(p.tenant_id, p.legal_entity_id) for p in policies]
            if len(scopes) != len(set(scopes)):
                raise FinanceError("CONFIGURATION_INVALID", 422, "合规制度公司重复")
            await self._configure_compliance_providers(conn, providers, simulation)
            for policy in policies:
                await self._configure_compliance_policy(conn, policy, simulation)
            for old in await conn.fetch("SELECT * FROM finance_compliance_policy WHERE enabled FOR UPDATE"):
                if (old["tenant_id"], old["legal_entity_id"]) not in scopes:
                    await conn.execute("UPDATE finance_compliance_policy SET enabled=false,generation=generation+1 WHERE tenant_id=$1 AND legal_entity_id=$2", old["tenant_id"], old["legal_entity_id"])
            await conn.execute("UPDATE finance_compliance_grant g SET active=false WHERE active AND NOT EXISTS(SELECT 1 FROM finance_compliance_plan p JOIN finance_compliance_policy c ON(c.tenant_id=p.tenant_id AND c.legal_entity_id=p.legal_entity_id) WHERE p.tenant_id=g.tenant_id AND p.legal_entity_id=g.legal_entity_id AND p.id=g.plan_id AND c.enabled AND c.generation=(p.data->'bindings'->'policy'->>'generation')::bigint)")

    async def _compliance_mode_config(self, conn: Connection, simulation: bool) -> None:
        """实例标记不能将已存在原件洗成新的隔离来源。"""
        marker = await conn.fetchval("SELECT simulation FROM finance_compliance_mode WHERE singleton FOR UPDATE")
        other = await conn.fetchval("SELECT simulation FROM finance_settlement_mode WHERE singleton")
        if (marker is not None and marker != simulation) or (other is not None and other != simulation):
            raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "独立财务库环境不能切换")
        if simulation and not str(await conn.fetchval("SELECT current_database()")).startswith("finance_test"):
            raise FinanceError("SIMULATION_DATABASE_REQUIRED", 422, "模拟仅允许独立finance_test数据库")
        await conn.execute("INSERT INTO finance_compliance_mode VALUES(true,$1) ON CONFLICT DO NOTHING", simulation)
        await conn.execute("INSERT INTO finance_settlement_mode VALUES(true,$1) ON CONFLICT DO NOTHING", simulation)

    async def _configure_compliance_policy(self, conn: Connection, policy: CompliancePolicy, simulation: bool) -> None:
        """保存不可变策略内容，重新启用仍增加代数。"""
        if policy.enabled and (policy.tax_enabled or policy.archive_enabled) and not simulation:
            raise FinanceError("COMPLIANCE_FORMAL_UNVERIFIED", 422, "正式税务供应商及归档标准profile尚未验收")
        for user in (set(policy.tax_executor_user_ids + policy.archive_executor_user_ids) if policy.enabled else set()):
            role = await conn.fetchval("SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE", policy.tenant_id, policy.legal_entity_id, user)
            if role != "reviewer":
                raise FinanceError("EXECUTOR_FORBIDDEN", 422, "合规经办必须是当前公司reviewer")
        for step in ([*policy.tax_steps, *policy.archive_steps] if policy.enabled else []):
            role = await conn.fetchval("SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE", policy.tenant_id, policy.legal_entity_id, step.approver_user_id)
            if role not in {"reviewer", "approver"}:
                raise FinanceError("APPROVER_FORBIDDEN", 422, "合规审批人员尚未取得公司岗位")
        data = policy.model_dump(mode="json", exclude={"enabled"})
        digest = request_digest(data)
        old = await conn.fetchval("SELECT digest FROM finance_compliance_policy_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND version=$3", policy.tenant_id, policy.legal_entity_id, policy.version)
        if old is not None and old != digest:
            raise FinanceError("POLICY_VERSION_CONFLICT", 409, "合规制度同版本内容不可覆盖")
        await conn.execute("INSERT INTO finance_compliance_policy_revision VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING", policy.tenant_id, policy.legal_entity_id, policy.version, digest, json.dumps(data))
        await conn.execute("INSERT INTO finance_compliance_policy(tenant_id,legal_entity_id,version,enabled) VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,legal_entity_id) DO UPDATE SET version=EXCLUDED.version,enabled=EXCLUDED.enabled,generation=CASE WHEN finance_compliance_policy.version<>EXCLUDED.version OR finance_compliance_policy.enabled<>EXCLUDED.enabled THEN finance_compliance_policy.generation+1 ELSE finance_compliance_policy.generation END", policy.tenant_id, policy.legal_entity_id, policy.version, policy.enabled)

    async def _configure_compliance_providers(self, conn: Connection, providers: list[dict[str, Any]], simulation: bool) -> None:
        """无凭据渠道描述同版本不可替换，删除只停用并保留查询能力。"""
        keys = [(p["provider"], p["configuration_version"]) for p in providers]
        if len(keys) != len(set(keys)):
            raise FinanceError("CONFIGURATION_INVALID", 422, "合规渠道版本重复")
        for data in providers:
            if data["simulation"] != simulation or data["kind"] not in {"tax", "archive"}:
                raise FinanceError("SIMULATION_MODE_CONFLICT", 422, "合规渠道环境或种类不符")
            body = {k: v for k, v in data.items() if k != "enabled"}
            digest = request_digest(body)
            old = await conn.fetchval("SELECT digest FROM finance_compliance_provider WHERE provider=$1 AND configuration_version=$2 FOR UPDATE", data["provider"], data["configuration_version"])
            if old is not None and old != digest:
                raise FinanceError("PROVIDER_VERSION_CONFLICT", 409, "合规渠道同版本内容不可替换")
            await conn.execute("INSERT INTO finance_compliance_provider(tenant_id,legal_entity_id,provider,configuration_version,kind,enabled,digest,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT(provider,configuration_version) DO UPDATE SET enabled=EXCLUDED.enabled,generation=CASE WHEN finance_compliance_provider.enabled<>EXCLUDED.enabled THEN finance_compliance_provider.generation+1 ELSE finance_compliance_provider.generation END", data["tenant_id"], data["legal_entity_id"], data["provider"], data["configuration_version"], data["kind"], data["enabled"], digest, json.dumps(body))
        for old in await conn.fetch("SELECT * FROM finance_compliance_provider WHERE enabled FOR UPDATE"):
            if (old["provider"], old["configuration_version"]) not in keys:
                active = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_compliance_operation WHERE provider=$1 AND configuration_version=$2 AND status IN ('accepted','running','dispatching','awaiting_provider','outcome_unknown'))", old["provider"], old["configuration_version"])
                if active:
                    raise FinanceError("PROVIDER_INFLIGHT", 409, "在途合规渠道配置不能移除")
                await conn.execute("UPDATE finance_compliance_provider SET enabled=false,generation=generation+1 WHERE provider=$1 AND configuration_version=$2", old["provider"], old["configuration_version"])

    async def _compliance_policy(self, conn: Connection, tenant: str, entity: str, *, require: bool = True) -> dict[str, Any] | None:
        """读取当前策略和代数；未配置永远按关闭处理。"""
        row = await conn.fetchrow("SELECT p.*,r.digest,r.data FROM finance_compliance_policy p JOIN finance_compliance_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=$1 AND p.legal_entity_id=$2 FOR SHARE OF p", tenant, entity)
        if row is None or not row["enabled"]:
            if require:
                raise FinanceError("COMPLIANCE_DISABLED", 409, "公司合规能力未启用")
            return None
        return {**decode(row["data"]), "enabled": row["enabled"], "generation": row["generation"], "digest": row["digest"]}

    async def _compliance_provider(self, conn: Connection, policy: dict[str, Any], kind: str) -> dict[str, Any]:
        """新发送严格复核公司、环境、版本、幂等和查询能力。"""
        if not policy[kind + "_enabled"]:
            raise FinanceError("COMPLIANCE_DISABLED", 409, "对应合规能力未启用")
        row = await conn.fetchrow("SELECT * FROM finance_compliance_provider WHERE tenant_id=$1 AND legal_entity_id=$2 AND provider=$3 AND configuration_version=$4 AND kind=$5 FOR SHARE", policy["tenant_id"], policy["legal_entity_id"], policy[kind + "_provider"], policy[kind + "_configuration_version"], kind)
        if row is None or not row["enabled"]:
            raise FinanceError("PROVIDER_UNAVAILABLE", 409, "合规渠道未配置或已停用")
        data = decode(row["data"])
        if not data["supports_idempotency"] or not data["supports_query"] or (kind == "tax" and not data.get("supports_state_revision")):
            raise FinanceError("PROVIDER_CAPABILITY_REQUIRED", 409, "渠道不满足幂等、查单或权威版本要求")
        return {**data, "generation": row["generation"], "digest": row["digest"]}
