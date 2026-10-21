"""启动时校验合规政策与公司、岗位及固定通道版本的引用。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_finance.compliance.models import CompliancePolicy
    from xuanpu_finance.config import FinanceConfig


def validate_compliance_config(config: FinanceConfig) -> None:
    """缺失或停用配置不授予能力，启用配置必须具有明确授权。"""
    companies = {(c.tenant_id, c.legal_entity_id) for c in config.companies}
    providers = {(p.provider, p.configuration_version): p for p in config.compliance_providers}
    keys = [(p.tenant_id, p.legal_entity_id) for p in config.compliance_policies]
    if len(providers) != len(config.compliance_providers) or len(keys) != len(set(keys)):
        raise ValueError("合规政策或通道版本重复")
    for provider in config.compliance_providers:
        if (provider.tenant_id, provider.legal_entity_id) not in companies or (
            provider.simulation != config.execution_simulation):
            raise ValueError("合规通道公司或隔离环境不符")
    for policy in config.compliance_policies:
        if (policy.tenant_id, policy.legal_entity_id) not in companies:
            raise ValueError("合规政策指向未配置公司")
        if policy.enabled:
            _validate_policy(config, policy)


def _validate_policy(config: FinanceConfig, policy: CompliancePolicy) -> None:
    """经办和审批资格来自当前公司授权，渠道不由用户选择URL。"""
    grants = {g.user_id: g.role for g in config.grants
              if (g.tenant_id, g.legal_entity_id) == (policy.tenant_id, policy.legal_entity_id)}
    providers = {(p.provider, p.configuration_version): p for p in config.compliance_providers}
    for kind in ("tax", "archive"):
        if not getattr(policy, kind + "_enabled"):
            continue
        people = getattr(policy, kind + "_executor_user_ids")
        steps = getattr(policy, kind + "_steps")
        if any(grants.get(user) != "reviewer" for user in people) or any(
            grants.get(step.approver_user_id) not in {"reviewer", "approver"} for step in steps):
            raise ValueError("合规经办或审批人员缺少本公司当前岗位")
        selector = (getattr(policy, kind + "_provider"),
                    getattr(policy, kind + "_configuration_version"))
        provider = providers.get(selector)
        if provider is None or not provider.enabled or provider.kind != kind or (
            provider.tenant_id, provider.legal_entity_id) != (policy.tenant_id, policy.legal_entity_id):
            raise ValueError("合规政策引用缺失、停用或异类通道")
        if kind == "archive" and config.execution_simulation and policy.required_lock_mode != "SIMULATION":
            raise ValueError("隔离归档必须明确使用SIMULATION锁策略")
