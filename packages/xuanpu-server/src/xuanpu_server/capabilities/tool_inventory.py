"""复用已有沙盒与连接器列工具，只预检不调用任何业务工具。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import taifeng

from xuanpu_server.errors import RuntimeCleanupRequiredError, RuntimeUnavailableError
from xuanpu_server.ids import new_id
from xuanpu_server.runtime.connectors import ConnectorSet
from xuanpu_server.runtime.events import SubmissionIndex

from .delivery import ToolDeclaration, fingerprint
from .probe_resources import ProbeResources

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.packages import ResolvedPackage
    from xuanpu_server.runtime.assembler import RuntimeDefaults
    from xuanpu_server.runtime.sandbox import SandboxProvider
    from xuanpu_server.secrets import SecretVault


class ToolInventory:
    """每次预检独立资源；不复用普通员工引擎或调用模型。"""
    def __init__(self, sandboxes: SandboxProvider, audit: AuditLog, data_dir: Path,
                 defaults: RuntimeDefaults) -> None:
        """所有执行配置与路径由平台构造时注入。"""
        self.sandboxes, self.audit, self.data_dir, self.defaults = sandboxes, audit, data_dir, defaults
        self.resources = ProbeResources(data_dir / 'capability-probes', sandboxes)
        self._lock = asyncio.Lock()
        self._closed = False

    def policy(self) -> dict[str, object]:
        """仅保留明确的运行策略，不序列化模型配置或秘密环境值。"""
        return {'sandbox_provider': type(self.sandboxes).__qualname__,
            'max_tokens': self.defaults.max_session_tokens, 'max_iterations': self.defaults.max_iterations,
            'connector_timeout': self.defaults.connector_timeout_seconds,
            'startup_timeout': self.defaults.connector_startup_seconds}

    async def collect(self, tenant: str, package: ResolvedPackage, vault: SecretVault,
                      *, files: bool) -> tuple[tuple[ToolDeclaration, ...], str]:
        """启动失败也执行全部收尾；只有实际工具列表进入权限摘要。"""
        async with self._lock:
            if self._closed:
                raise RuntimeUnavailableError('能力预检正在关闭')
            await self.resources.recover()
            return await self._collect(tenant, package, vault, files=files)

    async def _collect(self, tenant: str, package: ResolvedPackage, vault: SecretVault,
                       *, files: bool) -> tuple[tuple[ToolDeclaration, ...], str]:
        """预检不依赖员工引擎，失败时立即保存未交接资源凭证。"""
        root = self.data_dir / 'capability-probes' / new_id('probe')
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=False)
        self.resources.pending[root] = []
        try:
            try:
                if files or package.manifest.spec.policy.sandbox.shell:
                    sandbox = await self.sandboxes.open(package, root / 'scripts')
                    self.resources.add(root, sandbox.close)
                handle = None
                if any(c.transport == 'stdio' for c in package.manifest.spec.connectors):
                    handle = await self.sandboxes.open_connectors(package, root / 'connectors')
                    self.resources.add(root, handle.close)
            except RuntimeCleanupRequiredError as exc:
                self.resources.add(root, exc.cleanup)
                raise
            connectors = ConnectorSet(tenant_id=tenant, vault=vault, audit=self.audit,
                submissions=SubmissionIndex(), executor=handle.executor if handle else None,
                base_env=handle.process_env(self.defaults.connector_env) if handle else {}, strict_cleanup=True,
                startup_timeout_seconds=self.defaults.connector_startup_seconds,
                call_timeout_seconds=self.defaults.connector_timeout_seconds)
            self.resources.add(root, connectors.close)
            for connector in package.manifest.spec.connectors:
                await connectors.start(connector, package_root=package.root)
            tools = connectors.build_tools(taifeng.PermissionPolicy(rules=[], default_mode='deny'))
            selected = tuple(sorted((ToolDeclaration(name=t.spec.name,
                schema_sha256=fingerprint(t.spec.input_schema), effect=t.effect) for t in tools), key=lambda t: t.name))
            return selected, connectors.inventory_digest()
        finally:
            await self.resources.cleanup(root)

    async def recover(self) -> None:
        """下次预检或启动前先清理前次失败；失败不冒充已就绪。"""
        async with self._lock:
            await self.resources.recover()

    async def close(self) -> None:
        """关闭后不再接受新探测，并等待在途退出和遗留清理。"""
        async with self._lock:
            self._closed = True
            await self.resources.recover()
