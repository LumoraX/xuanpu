"""运行时组装：为「租户 + 业务包 + 版本」组装一个内核引擎池。

内核不知道租户（红线 2）。租户隔离体现在组装时注入的每一样资源上（红线 5）：
会话存储（数据目录下的专属目录，或状态库里的专属表）、脚本沙盒与工作区、连接器沙盒与
连接器工作目录、连接器凭据、模型凭据、权限策略，全部按租户分开。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import taifeng

from xuanpu_server.errors import RuntimeCleanupRequiredError, RuntimeUnavailableError
from xuanpu_server.runtime.approval import build_policy
from xuanpu_server.runtime.connectors import ConnectorSet
from xuanpu_server.runtime.execution_scope import ExecutionScope
from xuanpu_server.runtime.sandbox import DEFAULT_BASE_ENV
from xuanpu_server.runtime.script_executors import ScriptObserver, script_executors
from xuanpu_server.tenancy import tenant_directory

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from xuanpu_spec.manifest import Effect

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.packages import ResolvedPackage
    from xuanpu_server.runtime.events import SubmissionIndex
    from xuanpu_server.runtime.models import ModelClientFactory
    from xuanpu_server.runtime.sandbox import SandboxHandle, SandboxProvider
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)

RuntimeKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class RuntimeDefaults:
    """平台给运行时的默认参数。业务包里声明了预算的，以业务包为准，但不得超过平台上限。

    Attributes:
        max_session_tokens: 单个会话的 token 上限。
        max_iterations: 单轮对话里模型与工具往返的次数上限。
        connector_timeout_seconds: 连接器单次调用的超时。
        connector_startup_seconds: 启动一个 stdio 连接器（起进程、握手、列出工具）的时限。
        connector_env: 连接器进程的基础环境；None 时用最小环境 ``DEFAULT_BASE_ENV``。
            上面再加指向连接器工作目录的 ``HOME`` / ``TMPDIR`` / ``WORKSPACE``，以及清单
            声明的变量与密钥。
        capture_requests: 是否把完整提示词写进审计。
        session_idle_seconds: 会话多久没有对话就释放它的引擎（会话本身保持活动，下次
            对话时从存储续接）。
        runtime_idle_seconds: 运行时多久没有会话使用就回收（连接器进程、沙盒、引擎池
            一起关掉，下次使用时重新组装）。
    """

    max_session_tokens: int = 2_000_000
    max_iterations: int = 60
    connector_timeout_seconds: float = 60.0
    connector_startup_seconds: float = 30.0
    connector_env: Mapping[str, str] | None = None
    capture_requests: bool = False
    session_idle_seconds: float = 300.0
    runtime_idle_seconds: float = 900.0


@dataclass
class StateBackends:
    """一个租户的状态面后端。None 表示用内核的本机默认实现。

    Attributes:
        thread_directory: 线程目录。给了 ``message_store`` 时应与之配套（同一个库、同一个
            命名空间），否则线程列表与实际存的消息对不上；这时不给，内核用空目录
            （``NullThreadDirectory``）。两者都不给时用数据目录下的本机索引。
        index_hook: 线程生命周期的订阅方（例如把消息镜像到别处）。
        memory_store: 长期记忆。
        message_store: 会话主存的工厂，每个运行时调用一次、得到该运行时引擎池专用的存储
            实例（池关闭时关闭它）。给了它，会话主存就在外部存储里，不再用数据目录，
            多实例接手会话也就不需要共享数据目录。不能与 ``index_hook`` 同时给：外部
            主存与消息镜像同时挂上，多半是两者写同一张表、每条消息写两遍。
    """

    thread_directory: taifeng.ThreadDirectory | None = None
    index_hook: taifeng.IndexHook | None = None
    memory_store: taifeng.MemoryStore | None = None
    message_store: Callable[[], taifeng.MessageStore] | None = None

    def __post_init__(self) -> None:
        """拒绝把外部主存与消息镜像一起挂上。"""
        if self.message_store is not None and self.index_hook is not None:
            raise ValueError(
                "message_store 与 index_hook 不能同时给出：会话主存已在外部存储里，"
                "再挂消息镜像会把同一批消息写两遍"
            )


@runtime_checkable
class StateBackendFactory(Protocol):
    """给租户准备状态面后端。"""

    async def for_tenant(self, tenant_id: str) -> StateBackends:
        """返回该租户的后端；按租户隔离（库、schema 或前缀）由实现保证。"""
        ...


class LocalStateBackends:
    """全部用内核的本机默认实现：适合单机部署与本地试用。"""

    async def for_tenant(self, tenant_id: str) -> StateBackends:
        """不注入任何外部后端。"""
        return StateBackends()


async def _close_in_order(
    key: RuntimeKey, steps: list[tuple[str, Callable[[], Awaitable[None]]]], *, strict: bool = False
) -> None:
    """按顺序收尾。某一步失败只记日志，不影响后面的收尾。"""
    errors: list[BaseException] = []
    for name, closer in steps:
        try:
            await closer()
        except BaseException as exc:
            errors.append(exc)
            logger.exception("关闭运行时 %s 的%s失败", key, name)
    if strict and errors:
        raise BaseExceptionGroup("运行时资源未完全关闭", errors)


def _release_steps(
    connectors: ConnectorSet | None,
    connector_sandbox: SandboxHandle | None,
    sandbox: SandboxHandle | None,
) -> list[tuple[str, Callable[[], Awaitable[None]]]]:
    """引擎池之外的收尾顺序：先关连接器进程，再关它所在的连接器沙盒，最后关脚本沙盒。"""
    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    if connectors is not None:
        steps.append(("连接器", connectors.close))
    if connector_sandbox is not None:
        steps.append(("连接器沙盒", connector_sandbox.close))
    if sandbox is not None:
        steps.append(("沙盒", sandbox.close))
    return steps


@dataclass
class TenantRuntime:
    """一个已组装好的运行时。

    Attributes:
        connector_sandbox: stdio 连接器进程所在的沙盒；清单里没有 stdio 连接器时为 None。
    """

    key: RuntimeKey
    package: ResolvedPackage
    pool: taifeng.EnginePool
    sandbox: SandboxHandle
    connectors: ConnectorSet
    connector_sandbox: SandboxHandle | None = None
    tool_effects: dict[str, Effect] = field(default_factory=dict)
    last_used: float = 0.0
    retainers: int = 0
    closing: bool = False

    def live_sessions(self) -> int:
        """引擎池里当前活着的会话数。"""
        return len(self.pool.introspect())

    async def close(self) -> None:
        """依次关闭引擎池、连接器、连接器沙盒、脚本沙盒。某一步失败不影响后面的收尾。"""
        self.closing = True
        await _close_in_order(
            self.key,
            [
                ("引擎池", self.pool.close),
                *_release_steps(self.connectors, self.connector_sandbox, self.sandbox),
            ], strict=True,
        )


class RuntimeAssembler:
    """按业务包组装运行时，并缓存已组装的运行时。"""

    def __init__(
        self,
        *,
        data_dir: Path,
        models: ModelClientFactory,
        sandboxes: SandboxProvider,
        states: StateBackendFactory,
        vault: SecretVault,
        audit: AuditLog,
        submissions: SubmissionIndex,
        defaults: RuntimeDefaults | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            data_dir: 平台数据目录。
            models: 模型客户端工厂。
            sandboxes: 沙盒供给方。
            states: 状态面后端工厂。
            vault: 密钥保管库。
            audit: 审计写入口。
            submissions: submission → 会话的对照表。
            defaults: 平台默认参数。
            now: 取当前时间的函数，测试时可替换。
        """
        self._now = now
        self._submissions = submissions
        self._data_dir = data_dir
        self._models = models
        self._sandboxes = sandboxes
        self._states = states
        self._vault = vault
        self._audit = audit
        self._defaults = defaults or RuntimeDefaults()
        self._runtimes: dict[RuntimeKey, TenantRuntime] = {}
        self._locks: dict[RuntimeKey, asyncio.Lock] = {}
        self._state_locks: dict[Path, asyncio.Lock] = {}
        self._failed_closures: dict[RuntimeKey, list[tuple[str, Callable[[], Awaitable[None]]]]] = {}

    @property
    def defaults(self) -> RuntimeDefaults:
        """平台默认参数。"""
        return self._defaults

    def live_runtimes(self) -> int:
        """当前组装着（还没回收）的运行时数。"""
        return len(self._runtimes)

    def _scoped(self, area: str, package: ResolvedPackage, scope_key: str | None = None) -> Path:
        """某个运行时在数据目录下的专属目录。"""
        record = package.record
        tenant = tenant_directory(record.tenant_id)
        root = self._data_dir / area / tenant / record.name / record.version
        return root if scope_key is None else root / scope_key

    async def get(
        self, package: ResolvedPackage, scope: ExecutionScope | None = None, *, input_root: Path | None = None,
        script_observer: ScriptObserver | None = None, contains_external_input: bool = False
    ) -> TenantRuntime:
        """取运行时；尚未组装则组装。同一个运行时的并发请求只组装一次。"""
        record = package.record
        chosen = scope or ExecutionScope(record.tenant_id, "service:assembly", "package:" + record.name)
        if chosen.tenant_id != record.tenant_id:
            raise ValueError("执行空间与业务包租户不匹配")
        key: RuntimeKey = (record.tenant_id, record.name, record.version, chosen.storage_key())
        if key in self._failed_closures:
            raise RuntimeUnavailableError("执行空间上次装配尚未清理，不能重复启动")
        existing = self._runtimes.get(key)
        if existing is not None and existing.closing:
            raise RuntimeUnavailableError("执行空间尚未完成关闭，不能重复启动")
        if existing is None:
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                if key in self._failed_closures:
                    raise RuntimeUnavailableError("执行空间上次装配尚未清理，不能重复启动")
                existing = self._runtimes.get(key)
                if existing is None:
                    if chosen.execution_id.startswith("task:") and input_root is None:
                        raise RuntimeUnavailableError("任务运行时必须先固定输入只读挂载")
                    existing = await self._assemble(key, package, input_root=input_root,
                        contains_external_input=contains_external_input or input_root is not None or not chosen.owner_id.startswith("service:"), script_observer=script_observer)
                    self._runtimes[key] = existing
        if existing.closing:
            raise RuntimeUnavailableError("执行空间尚未完成关闭，不能重复启动")
        existing.last_used = self._now()
        return existing

    def _connector_set(
        self, package: ResolvedPackage, connector_sandbox: SandboxHandle | None
    ) -> ConnectorSet:
        """连接器集合。有连接器沙盒时，stdio 连接器经它的执行器起在沙盒里。

        连接器进程的环境 = 基础环境 + 指向连接器工作目录（沙盒里看到的路径）的
        ``HOME`` / ``TMPDIR`` / ``WORKSPACE`` + 清单声明的变量与密钥（后者由连接器集合加）。
        """
        configured = self._defaults.connector_env
        base = DEFAULT_BASE_ENV if configured is None else configured
        return ConnectorSet(
            tenant_id=package.record.tenant_id,
            vault=self._vault,
            audit=self._audit,
            submissions=self._submissions,
            executor=None if connector_sandbox is None else connector_sandbox.executor,
            base_env=base if connector_sandbox is None else connector_sandbox.process_env(base),
            call_timeout_seconds=self._defaults.connector_timeout_seconds,
            startup_timeout_seconds=self._defaults.connector_startup_seconds,
        )

    async def _assemble(
        self, key: RuntimeKey, package: ResolvedPackage, *, input_root: Path | None = None,
        contains_external_input: bool = False, script_observer: ScriptObserver | None = None,
    ) -> TenantRuntime:
        """组装一个运行时。任何一步失败都会收回已经准备好的资源。

        清单里有 stdio 连接器时，先为它们开连接器沙盒（工作目录在
        ``<data_dir>/connectors/<租户目录>/<名>/<版本>/``），没有就不开。
        """
        spec = package.manifest.spec
        connector_sandbox: SandboxHandle | None = None
        connectors: ConnectorSet | None = None
        sandbox: SandboxHandle | None = None
        try:
            if any(connector.transport == "stdio" for connector in spec.connectors):
                connector_sandbox = await self._sandboxes.open_connectors(
                    package, self._scoped("connectors", package, key[3])
                )
            connectors = self._connector_set(package, connector_sandbox)
            effects: dict[str, Effect] = {}
            for connector in spec.connectors:
                effects.update(await connectors.start(connector, package_root=package.root))
            workspace = self._scoped("workspaces", package, key[3])
            sandbox = (await self._sandboxes.open(package, workspace) if input_root is None
                else await self._sandboxes.open(package, workspace, input_root=input_root))
            policy = build_policy(spec.policy.approval, effects,
                contains_external_input=contains_external_input,
                workspace_root=sandbox.workspace_inside if input_root is not None else None)
            pool = await self._create_pool(package, policy, sandbox, connectors, key=key, script_observer=script_observer)
        except BaseException as original:
            steps = self._failed_closures.setdefault(key, [])
            if isinstance(original, RuntimeCleanupRequiredError):
                steps.append(("未移交容器", original.cleanup))
            steps.extend(_release_steps(connectors, connector_sandbox, sandbox))
            try:
                await _close_in_order(key, steps, strict=True)
            except BaseException as cleanup:
                raise BaseExceptionGroup("运行时装配失败且资源尚未清理", [original, cleanup]) from None
            del self._failed_closures[key]
            raise
        return TenantRuntime(
            key=key,
            package=package,
            pool=pool,
            sandbox=sandbox,
            connectors=connectors,
            connector_sandbox=connector_sandbox,
            tool_effects=effects,
        )

    def _extra_tools(
        self,
        package: ResolvedPackage,
        policy: taifeng.PermissionPolicy,
        sandbox: SandboxHandle,
        connectors: ConnectorSet,
    ) -> list[taifeng.ToolSpec]:
        """内核默认工具之外，平台额外开放的工具。"""
        tools = [tool.spec for tool in connectors.build_tools(policy)]
        if package.record.name == "office-assistant":
            from xuanpu_server.work_tasks.tools import office_tools

            tools.extend(office_tools(sandbox, policy))
        if package.manifest.spec.policy.sandbox.shell:
            registry = taifeng.BackgroundTaskRegistry(executor=sandbox.executor)
            tools.append(
                taifeng.make_run_in_background_tool(
                    registry=registry,
                    policy=policy,
                    cwd=sandbox.workspace_inside,
                    env=sandbox.process_env(),
                )
            )
            tools.append(taifeng.make_wait_for_task_tool(registry=registry))
        return tools

    async def _state_options(
        self,
        package: ResolvedPackage,
        states: StateBackends,
        store: taifeng.MessageStore | None,
    ) -> dict[str, Any]:
        """引擎池的状态面参数：会话主存放在哪里，外加线程目录、生命周期订阅、长期记忆。

        Args:
            package: 要组装的业务包。
            states: 该租户的状态面后端。
            store: 用状态面的工厂给本池造好的会话主存；None 表示会话主存是数据目录下的
                本机文件（这时才建 ``threads`` 目录）。
        """
        options: dict[str, Any] = {}
        if store is not None:
            options["message_store"] = store
        else:
            storage = self._scoped("threads", package)
            await asyncio.to_thread(storage.mkdir, parents=True, exist_ok=True)
            options["storage_dir"] = storage
        if states.thread_directory is not None:
            options["thread_directory"] = states.thread_directory
        if states.index_hook is not None:
            options["index_hook"] = states.index_hook
        if states.memory_store is not None:
            options["memory_store"] = states.memory_store
        return options

    async def _create_pool(
        self,
        package: ResolvedPackage,
        policy: taifeng.PermissionPolicy,
        sandbox: SandboxHandle,
        connectors: ConnectorSet,
        *,
        key: RuntimeKey, script_observer: ScriptObserver | None = None,
    ) -> taifeng.EnginePool:
        """创建内核引擎池。"""
        tenant_id = package.record.tenant_id
        spec = package.manifest.spec
        budget = spec.policy.budget
        defaults = self._defaults
        states = await self._states.for_tenant(tenant_id)
        store: taifeng.MessageStore | None = None
        try:
            if states.message_store is not None:
                store = states.message_store()
            else:
                # 多个scope共享既有消息位置；在第一次建索引时串行预热公开store接口。
                storage = self._scoped("threads", package)
                await asyncio.to_thread(storage.mkdir, parents=True, exist_ok=True)
                async with self._state_locks.setdefault(storage, asyncio.Lock()):
                    store = taifeng.JsonlMessageStore(storage)
                    await store.list_threads(limit=1)
            options = await self._state_options(package, states, store)
            return await taifeng.EnginePool.create(
                skills_dir=package.skills_dir,
                model_client=await self._models.create(tenant_id, spec.model.tier),
                extra_tools=self._extra_tools(package, policy, sandbox, connectors),
                script_executors=script_executors(sandbox, script_observer),
                permission_policy=policy,
                # 业务包可以把预算调低，不能调高过平台上限
                max_session_tokens=min(
                    budget.max_session_tokens or defaults.max_session_tokens,
                    defaults.max_session_tokens,
                ),
                max_iterations=min(
                    budget.max_iterations or defaults.max_iterations, defaults.max_iterations
                ),
                enable_request_capture=defaults.capture_requests,
                image_input_policy=taifeng.ImageInputPolicy(enabled=True, max_images=8, max_item_bytes=20*1024*1024, max_total_bytes=20*1024*1024),
                file_input_policy=taifeng.FileInputPolicy(enabled=True, max_files=8, max_item_bytes=20*1024*1024, max_total_bytes=20*1024*1024),
                **options,
            )
        except BaseException as original:
            # 池建成之前，注入的存储仍归我们（taifeng ADR 0111）：建池失败要自己关掉
            if store is not None:
                try:
                    await store.close()
                except BaseException as cleanup:
                    self._failed_closures.setdefault(key, []).append(("未移交会话主存", store.close))
                    raise BaseExceptionGroup("建池失败且主存尚未关闭", [original, cleanup]) from None
            raise

    async def recover_scope(self, package: ResolvedPackage, scope: ExecutionScope) -> None:
        """独占进程重启时回收固定Task目录的已知资源，不恢复执行。"""
        key = scope.storage_key()
        await self._sandboxes.close_workspace(self._scoped('workspaces', package, key))
        await self._sandboxes.close_workspace(self._scoped('connectors', package, key))

    async def release_scope(self, key: RuntimeKey) -> None:
        """任务持有者退出后关闭该独立执行空间，失败仍保留资源责任。"""
        async with self._locks.setdefault(key, asyncio.Lock()):
            runtime = self._runtimes.get(key)
            if runtime is None:
                return
            if runtime.retainers:
                raise RuntimeUnavailableError("任务运行时仍被持有")
            await runtime.close()
            del self._runtimes[key]

    async def evict_idle(self) -> list[RuntimeKey]:
        """回收没有会话在用、且空闲超过 ``runtime_idle_seconds`` 的运行时，返回回收了哪些。"""
        deadline = self._now() - self._defaults.runtime_idle_seconds
        evicted: list[RuntimeKey] = []
        for key, runtime in list(self._runtimes.items()):
            if runtime.last_used <= deadline and runtime.live_sessions() == 0 and runtime.retainers == 0:
                lock = self._locks.setdefault(key, asyncio.Lock())
                async with lock:
                    # 拿到锁之后再核对一次：这期间可能刚有会话进来
                    current = self._runtimes.get(key)
                    if current is runtime and runtime.live_sessions() == 0 and runtime.retainers == 0:
                        runtime.closing = True
                        await runtime.close()
                        del self._runtimes[key]
                        evicted.append(key)
        return evicted

    async def close(self) -> None:
        """关闭全部运行时。"""
        errors: list[BaseException] = []
        for key, runtime in list(self._runtimes.items()):
            try:
                await runtime.close()
            except BaseException as exc:
                errors.append(exc)
            else:
                del self._runtimes[key]
        for key, steps in list(self._failed_closures.items()):
            try:
                await _close_in_order(key, steps, strict=True)
            except BaseException as exc:
                errors.append(exc)
            else:
                del self._failed_closures[key]
        if errors:
            raise BaseExceptionGroup("部分运行时清理失败", errors)


__all__ = [
    "LocalStateBackends",
    "RuntimeAssembler",
    "RuntimeDefaults",
    "RuntimeKey",
    "StateBackendFactory",
    "StateBackends",
    "TenantRuntime",
]
