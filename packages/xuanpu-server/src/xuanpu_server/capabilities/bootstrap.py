"""能力基础装配不触发模型、网络或安装执行。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.runtime.assembler import RuntimeDefaults

from .preflight import Preflight
from .service import CapabilityService
from .tool_inventory import ToolInventory

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.organization.service import OrganizationService
    from xuanpu_server.packages import PackageRegistry
    from xuanpu_server.runtime.sandbox import SandboxProvider
    from xuanpu_server.secrets import SecretVault

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
