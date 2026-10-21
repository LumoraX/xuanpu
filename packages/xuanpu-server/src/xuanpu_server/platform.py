"""平台：把控制面与运行时的各个部件装配到一起。

接入层（HTTP、命令行）只和 ``Platform`` 打交道。所有外部依赖——存储、密钥保管库、
模型客户端工厂、沙盒供给方、状态面后端——都由构造参数注入，平台代码不读环境变量。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xuanpu_server.assistant import AssistantRouter
from xuanpu_server.audit import AuditLog
from xuanpu_server.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.evals import EvalRunner
from xuanpu_server.identity.feishu import DEFAULT_TIMEOUT_SECONDS, FeishuClient
from xuanpu_server.identity.listeners import CLOSE_SESSIONS, CloseUserSessions
from xuanpu_server.identity.service import IdentityService
from xuanpu_server.ids import new_id
from xuanpu_server.manual_connectors import ManualConnectorService
from xuanpu_server.metrics import PlatformMetrics
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.packages import PackageRegistry
from xuanpu_server.proactive.service import ProactiveService
from xuanpu_server.proactive.worker import ProactiveWorker
from xuanpu_server.quota import QuotaGuard
from xuanpu_server.reminders.feishu import FeishuNotificationSender
from xuanpu_server.reminders.service import ReminderService
from xuanpu_server.runtime.assembler import (
    LocalStateBackends,
    RuntimeAssembler,
    RuntimeDefaults,
    StateBackendFactory,
)
from xuanpu_server.runtime.call_context import CallContextSigner, HumanContextSigner
from xuanpu_server.runtime.connector_process import install_kernel_mcp_log_guard
from xuanpu_server.runtime.events import SubmissionIndex
from xuanpu_server.runtime.sessions import SessionManager
from xuanpu_server.tenancy import (
    ApiKeyRecord,
    Principal,
    QuotaLimits,
    Role,
    Tenant,
    generate_api_key,
    parse_api_key,
    validate_tenant_id,
    verify_metrics_token,
    verify_root_key,
    verify_secret,
)
from xuanpu_server.work_tasks.artifacts import ArtifactService
from xuanpu_server.work_tasks.inputs import InputSnapshotService
from xuanpu_server.work_tasks.proposals import ActionProposalService
from xuanpu_server.work_tasks.registry import TaskRegistry
from xuanpu_server.work_tasks.runner import TaskRunner
from xuanpu_server.work_tasks.service import WorkTaskService
from xuanpu_server.workspace.service import WorkspaceService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

    from xuanpu_server.config import CallContextConfig
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.runtime.instance_guard import InstanceGuard
    from xuanpu_server.runtime.models import ModelClientFactory
    from xuanpu_server.runtime.sandbox import SandboxProvider
    from xuanpu_server.secrets import SecretVault

ROOT_PRINCIPAL = Principal(role="platform_admin", tenant_id=None, key_id="root")


@dataclass(frozen=True)
class IssuedKey:
    """刚签发的 API key。完整的 key 只在这里出现一次。"""

    key: str
    record: ApiKeyRecord


from xuanpu_server.runtime.instance_guard import FileInstanceGuard

logger = logging.getLogger(__name__)


class Platform:
    """平台的装配结果。"""

    def __init__(
        self,
        *,
        store: ControlPlaneStore,
        identity_store: IdentityStore,
        identity_settings: IdentitySettings,
        vault: SecretVault,
        models: ModelClientFactory,
        sandboxes: SandboxProvider,
        data_dir: Path,
        root_key_hash: str,
        metrics_token_hash: str | None = None,
        call_context: CallContextConfig | None = None,
        instance_guard: InstanceGuard | None = None,
        objects: ObjectStore | None = None,
        states: StateBackendFactory | None = None,
        defaults: RuntimeDefaults | None = None,
        instance_id: str | None = None,
        lease_seconds: float = 60.0,
        shutdown_drain_seconds: float = 0.0,
        feishu_transport: httpx.AsyncBaseTransport | None = None,
        manual_http_transport: httpx.AsyncBaseTransport | None = None,
        proactive_interval: float = 1800.0,
        proactive_scan_seconds: float = 60.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 控制面存储。
            identity_store: 身份存储（员工、登录会话等），与控制面同一后端。
            identity_settings: 身份域的平台级设置（``public_url`` 与 ``identity`` 配置段，
                经 ``IdentitySettings.from_config`` 构造）。
            vault: 密钥保管库。
            models: 模型客户端工厂。
            sandboxes: 沙盒供给方。
            data_dir: 平台数据目录（业务包的本地缓存、工作区；local 状态面下还有会话
                存储）。只能归一个进程独占，不能被多个进程共用：业务包缓存的锁只在进程内，
                启动时还会清掉暂存目录的残留。
            root_key_hash: 平台根凭据的摘要。
            metrics_token_hash: 指标抓取令牌的摘要；None 不开指标端点。指标本身总在计，
                只是不对外。
            call_context: 可选的部署级 HTTP 调用签名配置；None 不为连接器签发身份。
            objects: 对象存储，业务包归档的正本放在这里；None 用 ``<data_dir>/objects``
                下的本机目录（只适合单机试用，多实例必须注入共用的对象存储）。
            states: 状态面后端工厂；None 用内核的本机默认实现。
            defaults: 运行时默认参数。
            instance_id: 本实例的标识；None 则随机生成。
            lease_seconds: 会话租约时长。
            shutdown_drain_seconds: 关闭时给在途对话多少时间跑完。
            feishu_transport: 调飞书接口用的 httpx 传输；None 用默认的（直连飞书）。飞书的两个
                基地址来自 ``identity_settings``。
            now: 取当前时间的函数，测试时可替换。
        """
        # 内核 MCP 客户端会把连接器 stdout 上的非 JSON 行原样写进日志；平台装配时给这些
        # logger 挂上过滤器（只记长度、截断长参数，见 connector_process 模块说明）
        install_kernel_mcp_log_guard()
        self._instance_guard = instance_guard or FileInstanceGuard(data_dir)
        self._instance_guard.on_lost = self._ownership_lost
        self._started = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._failure_cleanup: asyncio.Task[None] | None = None
        self.store = store
        self.vault = vault
        self.data_dir = data_dir
        self.objects: ObjectStore = (
            objects if objects is not None else LocalObjectStore(data_dir / "objects")
        )
        self.instance_id = instance_id or new_id("inst")
        self._root_key_hash = root_key_hash
        self._metrics_token_hash = metrics_token_hash
        self._drain_seconds = shutdown_drain_seconds
        self._now = now
        # 三个 gauge 的回调在抓取时才调用：会话管理与运行时组装器在下面才建好
        self.metrics = PlatformMetrics(
            sessions_live=lambda: self.sessions.held_sessions(),
            turns_in_flight=lambda: self.sessions.in_flight_turns(),
            runtimes_live=lambda: self.runtimes.live_runtimes(),
        )
        self.audit = AuditLog(store, now=now, metrics=self.metrics)
        self.identity = IdentityService(
            store=identity_store,
            control_plane=store,
            settings=identity_settings,
            vault=vault,
            audit=self.audit,
            feishu=FeishuClient(
                identity_settings.feishu_open_base,
                identity_settings.feishu_accounts_base,
                transport=feishu_transport,
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            ),
            metrics=self.metrics,
            now=now,
        )
        self.notification_sender = FeishuNotificationSender(
            control=store, identity=identity_store, vault=vault,
            open_base=identity_settings.feishu_open_base, transport=feishu_transport,
        )
        self.reminders = ReminderService(
            store=store.reminder_store, control=store, identity=identity_store,
            audit=self.audit, sender=self.notification_sender, now=now,
        )
        self.workspace = WorkspaceService(
            store=store.workspace_store, control=store, identity=identity_store,
            objects=self.objects, now=now,
        )
        self.quota = QuotaGuard(store, now=now)
        self.packages = PackageRegistry(
            store, self.audit, data_dir=data_dir, objects=self.objects, now=now
        )
        self.manual_connectors = ManualConnectorService(packages=self.packages, vault=vault,
            audit=self.audit, signer=None if call_context is None else HumanContextSigner(
                config=call_context, vault=vault, now=now), transport=manual_http_transport)
        self.work_tasks = WorkTaskService(self.workspace, self.packages,
            process_owner=self.instance_id, now=now)
        submissions = SubmissionIndex()
        self.runtimes = RuntimeAssembler(
            data_dir=data_dir,
            models=models,
            sandboxes=sandboxes,
            states=states or LocalStateBackends(),
            vault=vault,
            audit=self.audit,
            submissions=submissions,
            context_signer=None if call_context is None else CallContextSigner(
                config=call_context, store=store, vault=vault, now=now),
            defaults=defaults,
            now=now,
        )
        self.sessions = self._session_manager(submissions, lease_seconds)
        self.task_actions = ActionProposalService(self.workspace, self.work_tasks.store,
                                                 reminders=self.reminders, now=now)
        self.task_runner = TaskRunner(self.work_tasks.store, self.packages, self.runtimes, self.sessions,
            InputSnapshotService(self.workspace, data_dir / 'input-snapshots'),
            ArtifactService(self.workspace, self.work_tasks.store, now=now), self.task_actions)
        self.task_registry = TaskRegistry(self.work_tasks.store, self.task_runner,
            process_owner=self.instance_id, now=now, cleanup=self.task_runner.release,
            close_resources=self.task_runner.close, recover=self.task_runner.recover, actions=self.task_actions)
        self.work_tasks.executor = self.task_registry
        self.assistant = AssistantRouter(self, models, now=now)
        self.proactive = ProactiveService(workspace=self.workspace, control=store,
            identity=identity_store, sample=self.assistant.sample_proactive, now=now,
            semantic_interval=proactive_interval, reminders=self.reminders)
        self.proactive_worker = ProactiveWorker(self.proactive, scan_seconds=proactive_scan_seconds)
        # 员工停用后关掉他名下的会话：身份域不依赖运行时，监听者在这里接上会话管理
        self.identity.disabling.register("close_work_tasks", self.task_registry)
        self.identity.disabling.register(
            CLOSE_SESSIONS, CloseUserSessions(store=store, sessions=self.sessions)
        )
        self.evals = EvalRunner(self.sessions, self.packages, self.audit)

    def _session_manager(
        self, submissions: SubmissionIndex, lease_seconds: float
    ) -> SessionManager:
        """会话管理：用上已经建好的控制面部件、身份存储、运行时组装器与指标。"""
        return SessionManager(
            store=self.store,
            users=self.identity.store,
            packages=self.packages,
            runtimes=self.runtimes,
            quota=self.quota,
            audit=self.audit,
            submissions=submissions,
            metrics=self.metrics,
            instance_id=self.instance_id,
            lease_seconds=lease_seconds,
            now=self._now,
        )

    @property
    def available(self) -> bool:
        """丢失持有权或关闭后，不接受新的HTTP工作。"""
        return self._started and not self._closed and not self._instance_guard.lost

    def _ownership_lost(self) -> None:
        """同步停接单，再独立取消后台和在途工作；不能自动重连续跑。"""
        self.sessions.stop_accepting()
        if self._failure_cleanup is None:
            self._failure_cleanup = asyncio.create_task(self.close())
            self._failure_cleanup.add_done_callback(self._cleanup_finished)

    @staticmethod
    def _cleanup_finished(task: asyncio.Task[None]) -> None:
        """异常清理的失败必须可见，不能变成无人观察的任务异常。"""
        if not task.cancelled() and task.exception() is not None:
            logger.error("持有权丢失后的清理失败", exc_info=task.exception())

    async def start(self) -> None:
        """先获得单实例持有权，再启动全部后台工作。"""
        if self._started:
            return
        if self._closed:
            raise RuntimeError("已关闭的平台不能重新启动")
        await self._instance_guard.acquire()
        try:
            await self.packages.start()
            if self._closed or self._instance_guard.lost:
                raise RuntimeError("启动准备期间已关闭或丢失持有权")
            await self.sessions.start()
            await self.task_registry.open()
            self.identity.start()
            self.reminders.start()
            self.proactive_worker.start()
            self._started = True
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """全部后台、执行及审计清理后才释放持有权，重复关闭也等待同一次清理。"""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.sessions.stop_accepting()
            errors: list[BaseException] = []
            actions = (self.task_registry.close, self.proactive_worker.close, self.reminders.close,
                       self.notification_sender.close, self.assistant.close,
                       self.identity.maintenance.close, self.identity.disabling.close,
                       self._shutdown_sessions, self.runtimes.close, self.identity.close)
            for action in actions:
                try:
                    await action()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                # 无法证明工作已退场时保留持有权，必须由进程退出释放。
                raise BaseExceptionGroup("平台清理失败（持有权保留，需进程退出）", errors)
            await self._instance_guard.close()

    async def _shutdown_sessions(self) -> None:
        """丢锁时不等待业务排空，正常关闭按配置等待。"""
        await self.sessions.shutdown(drain_seconds=0 if self._instance_guard.lost else self._drain_seconds)

    # --- 认证 ---

    @property
    def metrics_enabled(self) -> bool:
        """是否开了指标端点（配了抓取令牌的摘要）。"""
        return self._metrics_token_hash is not None

    def verify_metrics_token(self, presented: str) -> bool:
        """校验指标抓取令牌；没开指标端点时总是 False。抓取令牌不是调用者身份。"""
        if self._metrics_token_hash is None:
            return False
        return verify_metrics_token(presented, self._metrics_token_hash)

    async def authenticate(self, presented: str, *, act_as: str | None = None) -> Principal:
        """由调用方出示的凭据（根凭据或 API key）得到调用者身份。

        各种失败（格式不对、不存在、已吊销、密钥不符、租户停用）对外是同一个提示，
        不让调用方据此探测凭据或租户是否存在。浏览器登录（cookie）不走这里，见
        ``IdentityService.authenticate_login``。

        Args:
            presented: 出示的凭据。
            act_as: 代哪名员工调用（请求头 ``X-Xuanpu-Act-As``）；None 不代。规则见
                ``IdentityService.act_as``；根凭据不能代员工调用。

        Raises:
            AuthenticationError: 凭据无效。
            PermissionDeniedError: 凭据有效，但不允许这次代员工调用。
        """
        if verify_root_key(presented, self._root_key_hash):
            if act_as is not None:
                raise PermissionDeniedError("根凭据不能代员工调用")
            return ROOT_PRINCIPAL
        record = await self._verify_api_key(presented)
        if act_as is not None:
            return await self.identity.act_as(record, act_as)
        return Principal(role=record.role, tenant_id=record.tenant_id, key_id=record.key_id)

    async def _verify_api_key(self, presented: str) -> ApiKeyRecord:
        """校验 API key，返回它的记录；key 所在的租户必须启用。

        Raises:
            AuthenticationError: 格式不对、不存在、已吊销、密钥不符、租户停用，同一句提示。
        """
        key_id, secret = parse_api_key(presented)
        record = await self.store.get_api_key(key_id)
        # 记录不存在时也做一次比较，让两种情况耗时相近
        matched = verify_secret(secret, record.secret_hash if record else "0" * 64)
        if record is None or not matched or not record.active:
            raise AuthenticationError("凭据无效")
        tenant = await self.store.get_tenant(record.tenant_id)
        if tenant is None or tenant.status != "active":
            raise AuthenticationError("凭据无效")
        return record

    # --- 租户与凭据 ---

    async def create_tenant(
        self,
        principal: Principal,
        *,
        tenant_id: str,
        name: str,
        quota: QuotaLimits | None = None,
    ) -> Tenant:
        """新建租户。"""
        principal.require_platform_admin()
        tenant = Tenant(
            tenant_id=validate_tenant_id(tenant_id),
            name=name,
            quota=quota or QuotaLimits(),
            created_at=self._now(),
        )
        await self.store.create_tenant(tenant)
        await self.audit.control(principal, tenant_id, "tenant_created", {"name": name})
        return tenant

    async def set_tenant_status(self, principal: Principal, tenant_id: str, status: str) -> Tenant:
        """启用或停用租户。停用后该租户的凭据立即失效。"""
        principal.require_platform_admin()
        if status not in ("active", "suspended"):
            raise ConflictError(f"未知的租户状态：{status!r}")
        tenant = await self.store.get_tenant(tenant_id)
        if tenant is None:
            raise NotFoundError(f"租户 {tenant_id!r} 不存在")
        updated = Tenant(
            tenant_id=tenant.tenant_id,
            name=tenant.name,
            status=status,  # type: ignore[arg-type]
            quota=tenant.quota,
            created_at=tenant.created_at,
        )
        await self.store.update_tenant(updated)
        await self.audit.control(principal, tenant_id, "tenant_status_changed", {"status": status})
        return updated

    async def set_tenant_quota(
        self, principal: Principal, tenant_id: str, quota: QuotaLimits
    ) -> Tenant:
        """整体替换租户的配额（没给的项就是不限）。配额每次检查都现读，改完立即生效。

        审计里记下改之前与改之后的三项。
        """
        principal.require_platform_admin()
        tenant = await self.store.get_tenant(tenant_id)
        if tenant is None:
            raise NotFoundError(f"租户 {tenant_id!r} 不存在")
        updated = dataclasses.replace(tenant, quota=quota)
        await self.store.update_tenant(updated)
        await self.audit.control(
            principal,
            tenant_id,
            "tenant_quota_changed",
            {"before": dataclasses.asdict(tenant.quota), "after": dataclasses.asdict(quota)},
        )
        return updated

    async def issue_key(
        self,
        principal: Principal,
        *,
        tenant_id: str | None,
        name: str,
        role: Role,
        act_as_users: bool = False,
    ) -> IssuedKey:
        """签发 API key。

        平台管理员可以给任何租户签发；租户管理员只能给自己的租户签发；租户成员不能签发（包括
        代员工调用的成员，其生效角色是成员）。``act_as_users`` 为真时授予代员工调用的能力
        （设计 6.3）。设计要求只有平台管理员与租户管理员能授予它，而签发本身已经只有这两种
        调用者能做，所以不另设规则；以后若放开成员签发，这里要单独拦住这项能力（有测试守着）。
        """
        if principal.role == "platform_admin":
            if tenant_id is None:
                raise ConflictError("平台管理员签发凭据时必须指定租户")
            target = tenant_id
        else:
            target = principal.require_tenant_admin()
            if tenant_id not in (None, target):
                raise NotFoundError("租户不存在")
        if role == "platform_admin":
            raise ConflictError("平台管理员身份只来自根凭据，不能签发")
        if await self.store.get_tenant(target) is None:
            raise NotFoundError(f"租户 {target!r} 不存在")
        key, key_id, secret_hash = generate_api_key()
        record = ApiKeyRecord(
            key_id=key_id,
            tenant_id=target,
            name=name,
            role=role,
            secret_hash=secret_hash,
            created_at=self._now(),
            can_act_as_user=act_as_users,
        )
        await self.store.create_api_key(record)
        await self.audit.control(
            principal,
            target,
            "api_key_issued",
            {"key_id": key_id, "name": name, "role": role, "act_as_users": act_as_users},
        )
        return IssuedKey(key=key, record=record)

    async def revoke_key(self, principal: Principal, key_id: str) -> bool:
        """吊销本租户的一把 API key。"""
        tenant_id = principal.require_tenant_admin()
        revoked = await self.store.revoke_api_key(tenant_id, key_id, at=self._now())
        if revoked:
            await self.audit.control(principal, tenant_id, "api_key_revoked", {"key_id": key_id})
        return revoked

    # --- 密钥 ---

    async def put_secret(self, principal: Principal, name: str, value: str) -> None:
        """写入或覆盖一条密钥。审计里只记名字。"""
        tenant_id = principal.require_tenant_admin()
        await self.vault.put(tenant_id, name, value, actor=principal.actor)
        await self.audit.control(principal, tenant_id, "secret_written", {"name": name})

    async def delete_secret(self, principal: Principal, name: str) -> bool:
        """删除一条密钥。"""
        tenant_id = principal.require_tenant_admin()
        deleted = await self.vault.delete(tenant_id, name)
        if deleted:
            await self.audit.control(principal, tenant_id, "secret_deleted", {"name": name})
        return deleted


__all__ = ["ROOT_PRINCIPAL", "IssuedKey", "Platform"]
