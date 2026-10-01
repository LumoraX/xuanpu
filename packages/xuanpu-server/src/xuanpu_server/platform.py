"""平台：把控制面与运行时的各个部件装配到一起。

接入层（HTTP、命令行）只和 ``Platform`` 打交道。所有外部依赖——存储、密钥保管库、
模型客户端工厂、沙盒供给方、状态面后端——都由构造参数注入，平台代码不读环境变量。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xuanpu_server.audit import AuditLog
from xuanpu_server.errors import AuthenticationError, ConflictError, NotFoundError
from xuanpu_server.evals import EvalRunner
from xuanpu_server.ids import new_id
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.packages import PackageRegistry
from xuanpu_server.quota import QuotaGuard
from xuanpu_server.runtime.assembler import (
    LocalStateBackends,
    RuntimeAssembler,
    RuntimeDefaults,
    StateBackendFactory,
)
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
    verify_root_key,
    verify_secret,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.runtime.models import ModelClientFactory
    from xuanpu_server.runtime.sandbox import SandboxProvider
    from xuanpu_server.secrets import SecretVault

ROOT_PRINCIPAL = Principal(role="platform_admin", tenant_id=None, key_id="root")


@dataclass(frozen=True)
class IssuedKey:
    """刚签发的 API key。完整的 key 只在这里出现一次。"""

    key: str
    record: ApiKeyRecord


class Platform:
    """平台的装配结果。"""

    def __init__(
        self,
        *,
        store: ControlPlaneStore,
        vault: SecretVault,
        models: ModelClientFactory,
        sandboxes: SandboxProvider,
        data_dir: Path,
        root_key_hash: str,
        objects: ObjectStore | None = None,
        states: StateBackendFactory | None = None,
        defaults: RuntimeDefaults | None = None,
        instance_id: str | None = None,
        lease_seconds: float = 60.0,
        shutdown_drain_seconds: float = 0.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 控制面存储。
            vault: 密钥保管库。
            models: 模型客户端工厂。
            sandboxes: 沙盒供给方。
            data_dir: 平台数据目录（业务包的本地缓存、工作区；local 状态面下还有会话
                存储）。只能归一个进程独占，不能被多个进程共用：业务包缓存的锁只在进程内，
                启动时还会清掉暂存目录的残留。
            root_key_hash: 平台根凭据的摘要。
            objects: 对象存储，业务包归档的正本放在这里；None 用 ``<data_dir>/objects``
                下的本机目录（只适合单机试用，多实例必须注入共用的对象存储）。
            states: 状态面后端工厂；None 用内核的本机默认实现。
            defaults: 运行时默认参数。
            instance_id: 本实例的标识；None 则随机生成。
            lease_seconds: 会话租约时长。
            shutdown_drain_seconds: 关闭时给在途对话多少时间跑完。
            now: 取当前时间的函数，测试时可替换。
        """
        # 内核 MCP 客户端会把连接器 stdout 上的非 JSON 行原样写进日志；平台装配时给这些
        # logger 挂上过滤器（只记长度、截断长参数，见 connector_process 模块说明）
        install_kernel_mcp_log_guard()
        self.store = store
        self.vault = vault
        self.data_dir = data_dir
        self.objects: ObjectStore = (
            objects if objects is not None else LocalObjectStore(data_dir / "objects")
        )
        self.instance_id = instance_id or new_id("inst")
        self._root_key_hash = root_key_hash
        self._drain_seconds = shutdown_drain_seconds
        self._now = now
        self.audit = AuditLog(store, now=now)
        self.quota = QuotaGuard(store, now=now)
        self.packages = PackageRegistry(
            store, self.audit, data_dir=data_dir, objects=self.objects, now=now
        )
        submissions = SubmissionIndex()
        self.runtimes = RuntimeAssembler(
            data_dir=data_dir,
            models=models,
            sandboxes=sandboxes,
            states=states or LocalStateBackends(),
            vault=vault,
            audit=self.audit,
            submissions=submissions,
            defaults=defaults,
            now=now,
        )
        self.sessions = SessionManager(
            store=store,
            packages=self.packages,
            runtimes=self.runtimes,
            quota=self.quota,
            audit=self.audit,
            submissions=submissions,
            instance_id=self.instance_id,
            lease_seconds=lease_seconds,
            now=now,
        )
        self.evals = EvalRunner(self.sessions, self.packages, self.audit)

    async def start(self) -> None:
        """清掉上次进程留下的暂存残留，再启动后台任务。"""
        await self.packages.start()
        await self.sessions.start()

    async def close(self) -> None:
        """停止接单、等在途对话跑完、释放会话、关闭运行时。"""
        await self.sessions.shutdown(drain_seconds=self._drain_seconds)
        await self.runtimes.close()

    # --- 认证 ---

    async def authenticate(self, presented: str) -> Principal:
        """由调用方出示的凭据得到调用者身份。

        各种失败（格式不对、不存在、已吊销、密钥不符、租户停用）对外是同一个提示，
        不让调用方据此探测凭据或租户是否存在。
        """
        if verify_root_key(presented, self._root_key_hash):
            return ROOT_PRINCIPAL
        key_id, secret = parse_api_key(presented)
        record = await self.store.get_api_key(key_id)
        # 记录不存在时也做一次比较，让两种情况耗时相近
        matched = verify_secret(secret, record.secret_hash if record else "0" * 64)
        if record is None or not matched or not record.active:
            raise AuthenticationError("凭据无效")
        tenant = await self.store.get_tenant(record.tenant_id)
        if tenant is None or tenant.status != "active":
            raise AuthenticationError("凭据无效")
        return Principal(role=record.role, tenant_id=record.tenant_id, key_id=record.key_id)

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
        self, principal: Principal, *, tenant_id: str | None, name: str, role: Role
    ) -> IssuedKey:
        """签发 API key。

        平台管理员可以给任何租户签发；租户管理员只能给自己的租户签发。
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
        )
        await self.store.create_api_key(record)
        await self.audit.control(
            principal, target, "api_key_issued", {"key_id": key_id, "name": name, "role": role}
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
