"""交付描述与已校验业务包的静态交叉核对。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError

if TYPE_CHECKING:
    from xuanpu_server.packages import ResolvedPackage
    from xuanpu_server.runtime.assembler import RuntimeDefaults

    from .delivery import Delivery


def validate_package(delivery: Delivery, package: ResolvedPackage, defaults: RuntimeDefaults) -> None:
    """摘要、入口及权限必须精确对应，不以描述覆盖清单。"""
    manifest = package.manifest
    policy = manifest.spec.policy
    permissions = delivery.permissions
    if (delivery.package.digest != package.record.digest or
            delivery.package.entry not in manifest.spec.skills.entries):
        raise ConflictError('交付描述与包摘要或入口不一致')
    expected = (policy.sandbox.shell, policy.sandbox.network, policy.approval,
                manifest.spec.model.tier, policy.budget.max_session_tokens or defaults.max_session_tokens,
                policy.budget.max_iterations or defaults.max_iterations)
    actual = (permissions.shell, permissions.network, permissions.approval,
              permissions.model_tier, permissions.max_tokens, permissions.max_iterations)
    if expected != actual:
        raise ConflictError('交付权限与包声明不一致；请明确预算、模型和审批需求')
    secrets = set()
    for connector in manifest.spec.connectors:
        if connector.transport in ('remote', 'stdio') and not connector.tools:
            raise ConflictError('纳管MCP能力必须明确列出工具，不能开放全部工具')
        if connector.auth:
            secrets.add(connector.auth.secret)
        secrets.update(value.secret for value in connector.env.values() if value.secret is not None)
    if secrets != set(delivery.requirements.secret_refs):
        raise ConflictError('秘密引用槽与包清单不一致')
    declared_connectors = {c.connector for c in permissions.connections}
    if not declared_connectors <= {c.name for c in manifest.spec.connectors}:
        raise ConflictError('连接要求不在包清单中')
