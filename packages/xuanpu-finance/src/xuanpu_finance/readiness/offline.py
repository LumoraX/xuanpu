"""只读取显式固定凭据文件的可取消离线检查，不创建任何运行资源。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
from cryptography.exceptions import UnsupportedAlgorithm

from xuanpu_finance.operations import request_digest
from xuanpu_finance.readiness.checks import build_report
from xuanpu_finance.readiness.credentials import (
    capture_credentials,
    tls_origin,
)
from xuanpu_finance.readiness.credentials import (
    read_fixed as read_fixed,
)
from xuanpu_finance.readiness.credentials import (
    read_text_secret as read_text_secret,
)
from xuanpu_finance.readiness.credentials import (
    valid_dsn as valid_dsn,
)
from xuanpu_finance.readiness.internal import internal_report
from xuanpu_finance.readiness.models import ReadinessCheck, ReadinessReport, check

if TYPE_CHECKING:
    from xuanpu_finance.config import FinanceConfig

def credential_resources(config: FinanceConfig) -> list[ReadinessCheck]:
    """捕获资源格式错误但永不输出路径、DSN、正文或异常消息。"""
    try:
        capture_credentials(config)
    except (OSError, ValueError, UnicodeError, UnsupportedAlgorithm):
        return [check("credential_resources", False, "credential_resources_invalid")]
    return [check("credential_resources", True, "credential_resources_valid")]


def offline_snapshot(config: FinanceConfig, tenant: str, entity: str) -> dict[str, Any]:
    """只在内存准备同公司配置快照，秘密文件引用不进入摘要。"""
    policies = {}
    for kind in ("expense", "settlement", "compliance", "funding"):
        for policy in getattr(config, kind + "_policies"):
            if (policy.tenant_id, policy.legal_entity_id) == (tenant, entity):
                policies[kind] = policy.model_dump(mode="json")
    return {
        "scope": {"tenant_id": tenant, "legal_entity_id": entity},
        "company": [
            c.model_dump()
            for c in config.companies
            if (c.tenant_id, c.legal_entity_id) == (tenant, entity)
        ],
        "roles": [
            g.model_dump()
            for g in config.grants
            if (g.tenant_id, g.legal_entity_id) == (tenant, entity)
        ],
        "policies": policies,
        "simulation": config.execution_simulation,
        "providers": [
            p.public_descriptor()
            for p in [*config.settlement_providers, *config.compliance_providers]
            if (p.tenant_id, p.legal_entity_id) == (tenant, entity)
        ],
        "catalog": [
            {**a.model_dump(mode="json"), "enabled": True}
            for a in config.payee_accounts
            if (a.tenant_id, a.legal_entity_id) == (tenant, entity)
        ],
    }


async def preflight(config: FinanceConfig) -> ReadinessReport:
    """不连接数据库、网关或供应商，不迁移也不启动worker。"""
    resources = await anyio.to_thread.run_sync(credential_resources, config)
    tls = config.environment != "production" or (
        tls_origin(config.gateway.base_url)
        and (config.objects.kind != "s3" or tls_origin(config.objects.endpoint_url or ""))
    )
    resources.append(check("transport_tls", tls, "正式网关与对象端点必须使用TLS"))
    resources.append(
        check(
            "gateway_credentials",
            {c.tenant_id for c in config.companies} <= set(config.gateway.tenant_token_files),
            "每个公司租户须有独立网关凭据",
        )
    )
    scopes = [(c.tenant_id, c.legal_entity_id) for c in config.companies] or [("", "")]
    snapshots = [offline_snapshot(config, *scope) for scope in scopes]
    if config.profile == "internal_records":
        return internal_report(config, snapshots, resources)
    reports = [build_report(snapshot, resources) for snapshot in snapshots]
    result = reports[0]
    if len(reports) > 1:
        result["checks"] = [c for r in reports for c in r["checks"]]
        result["configuration_digest"] = request_digest(
            [r["configuration_digest"] for r in reports]
        )
        if any(r["status"] == "blocked" for r in reports):
            result["status"] = "blocked"
    return result
