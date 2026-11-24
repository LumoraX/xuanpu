"""能力基础装配不触发模型、网络或安装执行。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from server.engine.runtime.assembler import RuntimeDefaults

from .preflight import Preflight
from .service import CapabilityService
from .tool_inventory import ToolInventory

if TYPE_CHECKING:
    from pathlib import Path

    from server.core.audit import AuditLog
    from server.core.objectstore import ObjectStore
    from server.core.secrets import SecretVault
    from server.domains.organization.service import OrganizationService
    from server.engine.packages import PackageRegistry
    from server.engine.runtime.sandbox import SandboxProvider

    from .config import CapabilityDeliveryConfig


def build_capabilities(organization: OrganizationService, packages: PackageRegistry, objects: ObjectStore,
                       vault: SecretVault, sandboxes: SandboxProvider, audit: AuditLog,
                       data_dir: Path, defaults: RuntimeDefaults | None,
                       config: CapabilityDeliveryConfig | None) -> CapabilityService:
    """注入同控制面的密钥与连接元数据，只供管理员显式预检时使用。"""
    inventory = ToolInventory(sandboxes, audit, data_dir, defaults or RuntimeDefaults())
    preflight = Preflight(organization.control, organization.control.connection_store,
                          packages, objects, vault, inventory)
    return CapabilityService(organization, packages, config, preflight=preflight)
