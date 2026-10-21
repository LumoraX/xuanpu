"""F2运行配置的公司、人员、通道和隔离环境交叉验证。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xuanpu_finance.config import FinanceConfig
    from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy
    from xuanpu_finance.settlement.provider_models import ProviderConfig


def validate_settlement_config(config: FinanceConfig) -> None:
    """结构检查不探测渠道，但拒绝配置歧义与执行越权。"""
    if config.environment == "production" and config.execution_simulation:
        raise ValueError("生产环境不能启用模拟付款或模拟记账")
    companies = {(item.tenant_id, item.legal_entity_id) for item in config.companies}
    grants = {
        (item.tenant_id, item.legal_entity_id, item.user_id): item.role for item in config.grants
    }
    providers = {
        (item.provider, item.configuration_version): item for item in config.settlement_providers
    }
    if len(providers) != len(config.settlement_providers):
        raise ValueError("通道及配置版本必须全实例唯一")
    for item in config.settlement_providers:
        if (item.tenant_id, item.legal_entity_id) not in companies:
            raise ValueError("通道必须属于已配置公司")
        if item.simulation != config.execution_simulation:
            raise ValueError("实例和通道的模拟环境必须一致")
    seen = set()
    for policy in config.settlement_policies:
        scope = (policy.tenant_id, policy.legal_entity_id)
        if scope not in companies or scope in seen:
            raise ValueError("执行策略公司不存在或重复")
        seen.add(scope)
        if policy.enabled:
            _validate_enabled_policy(config, policy, providers, grants)
    _validate_catalog(config, companies, grants)


def _validate_enabled_policy(
    config: FinanceConfig,
    policy: SettlementPolicy,
    providers: Mapping[tuple[str, int], ProviderConfig],
    grants: Mapping[tuple[str, str, str], str],
) -> None:
    """每种经办与实际批准人员必须有当前同公司财务授权。"""
    scope = (policy.tenant_id, policy.legal_entity_id)
    if not config.execution_simulation and not policy.require_verified_invoice:
        raise ValueError("正式执行策略必须要求实际有效查验")
    for user in [*policy.payment_executor_user_ids, *policy.posting_executor_user_ids]:
        if grants.get((*scope, user)) != "reviewer":
            raise ValueError("付款记账经办必须为本公司有效reviewer")
    for steps in [policy.payment_steps, policy.posting_steps, policy.reversal_steps]:
        if any(
            grants.get((*scope, step.approver_user_id)) not in {"reviewer", "approver"}
            for step in steps
        ):
            raise ValueError("专用审批链必须引用本公司有效审批人员")
    bindings = [
        (policy.payment_provider, policy.payment_configuration_version),
        (policy.posting_provider, policy.posting_configuration_version),
    ]
    for binding in bindings:
        provider = providers.get(binding)
        if provider is None or (provider.tenant_id, provider.legal_entity_id) != scope:
            raise ValueError("执行策略必须绑定本公司存在的通道版本")
        if not provider.enabled or not provider.supports_idempotency or not provider.supports_query:
            raise ValueError("启用执行必须有明确的幂等及查单通道")


def _validate_catalog(
    config: FinanceConfig,
    companies: set[tuple[str, str]],
    grants: Mapping[tuple[str, str, str], str],
) -> None:
    """受控资料和账户只能引用当前明确公司及人员范围。"""
    seen = set()
    catalog: list[BusinessSnapshot | PayeeAccount] = [
        *config.business_documents,
        *config.payee_accounts,
    ]
    for item in catalog:
        scope = (item.tenant_id, item.legal_entity_id)
        key = (*scope, item.ref.kind, item.ref.source, item.ref.id)
        if scope not in companies or key in seen:
            raise ValueError("主数据公司不存在或当前引用重复")
        seen.add(key)
        if any((*scope, user) not in grants for user in item.allowed_user_ids):
            raise ValueError("主数据范围必须引用当前有公司授权的人员")
