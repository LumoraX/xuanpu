"""控制面存储协议。

平台的持久状态都经这个协议读写。除了租户与凭据的管理方法，每个方法的第一个参数都是
``tenant_id``：调用方必须说清自己在哪个租户里操作，存储实现按它过滤，任何查询都不会
跨租户返回数据（红线 5）。

两个实现：``InMemoryControlPlane`` 用于测试与单机试用，``PostgresControlPlane`` 用于
多实例部署。二者对同一套契约测试负责。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from xuanpu_server.connections.store import ConnectionStore
    from xuanpu_server.controlplane.models import (
        AuditQuery,
        AuditRecord,
        PackageVersionRecord,
        SecretRecord,
        SessionRecord,
        UsageRecord,
    )
    from xuanpu_server.reminders.store import ReminderStore
    from xuanpu_server.runtime.receipt_store import ReceiptStore
    from xuanpu_server.tenancy import ApiKeyRecord, Tenant
    from xuanpu_server.workspace.store import WorkspaceStore


@runtime_checkable
class ControlPlaneStore(Protocol):
    """控制面存储。"""

    @property
    def connection_store(self) -> ConnectionStore:
        """与控制面同后端的员工连接与内部凭据存储。"""
        ...

    @property
    def workspace_store(self) -> WorkspaceStore:
        """与控制面共享事务域的个人工作台存储。"""
        ...

    @property
    def reminder_store(self) -> ReminderStore:
        """与控制面共享后端的提醒存储。"""
        ...

    @property
    def operation_receipts(self) -> ReceiptStore:
        """与控制面共享事务域的审批操作回执。"""
        ...

    # --- 租户与凭据 ---

    async def create_tenant(self, tenant: Tenant) -> None:
        """新建租户；标识已存在抛 ``ConflictError``。"""
        ...

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        """按标识取租户。"""
        ...

    async def list_tenants(self) -> list[Tenant]:
        """列出全部租户，按标识排序。"""
        ...

    async def update_tenant(self, tenant: Tenant) -> None:
        """整体替换租户记录；不存在抛 ``NotFoundError``。"""
        ...

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        """登记一把 API key。"""
        ...

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        """按标识取 API key。这是唯一不带租户的查询：认证发生在知道租户之前。"""
        ...

    async def list_api_keys(self, tenant_id: str) -> list[ApiKeyRecord]:
        """列出租户的 API key，按创建时间排序。"""
        ...

    async def revoke_api_key(self, tenant_id: str, key_id: str, *, at: float) -> bool:
        """吊销 API key；返回是否确实吊销了一把仍然有效的 key。"""
        ...

    # --- 业务包 ---

    async def add_package_version(self, record: PackageVersionRecord) -> None:
        """登记一个已发布版本；同名同版本已存在抛 ``ConflictError``。"""
        ...

    async def get_package_version(
        self, tenant_id: str, name: str, version: str
    ) -> PackageVersionRecord | None:
        """取某个版本。"""
        ...

    async def list_package_versions(
        self, tenant_id: str, name: str | None = None
    ) -> list[PackageVersionRecord]:
        """列出已发布版本，按发布时间排序。"""
        ...

    async def set_active_version(
        self, tenant_id: str, name: str, version: str, *, at: float
    ) -> None:
        """把某个已发布版本设为生效版本；版本不存在抛 ``NotFoundError``。"""
        ...

    async def get_active_version(self, tenant_id: str, name: str) -> str | None:
        """取生效版本号；没有设过返回 None。"""
        ...

    # --- 密钥 ---

    async def put_secret(self, record: SecretRecord) -> None:
        """写入或覆盖一条密钥。"""
        ...

    async def get_secret(self, tenant_id: str, name: str) -> SecretRecord | None:
        """取一条密钥。"""
        ...

    async def list_secret_names(self, tenant_id: str) -> list[str]:
        """列出密钥名，按名字排序。"""
        ...

    async def delete_secret(self, tenant_id: str, name: str) -> bool:
        """删除一条密钥；返回是否确实删掉了。"""
        ...

    # --- 会话 ---

    async def create_session(self, record: SessionRecord) -> None:
        """新建会话。"""
        ...

    async def get_session(self, tenant_id: str, session_id: str) -> SessionRecord | None:
        """取会话。"""
        ...

    async def list_sessions(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
        user_id: str | None = None,
    ) -> list[SessionRecord]:
        """列出会话，最近活动的在前；可按状态、创建者与所属员工（``user_id``）过滤。"""
        ...

    async def scan_user_sessions(
        self,
        tenant_id: str,
        user_id: str,
        after_id: str | None = None,
        limit: int = 5,
    ) -> list[SessionRecord]:
        """后台按会话标识升序有界扫描本人员工业务会话，与展示列表独立分页。"""
        ...

    async def count_active_sessions(self, tenant_id: str) -> int:
        """租户当前的活动会话数。"""
        ...

    async def update_session(
        self,
        tenant_id: str,
        session_id: str,
        *,
        at: float,
        thread_id: str | None = None,
        status: str | None = None,
    ) -> SessionRecord:
        """更新会话的线程标识或状态，并刷新活动时间；不存在抛 ``NotFoundError``。"""
        ...

    async def acquire_lease(
        self, tenant_id: str, session_id: str, *, owner: str, now: float, ttl_seconds: float
    ) -> SessionRecord:
        """取得或续期会话的持有权。

        无人持有、持有已到期、或持有者就是自己时成功，到期时刻延到 ``now + ttl_seconds``。
        整个判断与写入是原子的。

        Raises:
            NotFoundError: 会话不存在。
            SessionBusyError: 会话正由另一个实例持有且尚未到期。
        """
        ...

    async def release_lease(self, tenant_id: str, session_id: str, *, owner: str) -> bool:
        """放弃持有权；只有持有者自己能放弃。返回是否确实放弃了。"""
        ...

    # --- 用量 ---

    async def add_usage(
        self,
        tenant_id: str,
        day: str,
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        turns: int,
    ) -> UsageRecord:
        """把一笔用量累加到某一天，返回累加后的结果。累加是原子的。"""
        ...

    async def get_usage(self, tenant_id: str, day: str) -> UsageRecord:
        """取某一天的用量；没有记录时各项为 0。"""
        ...

    async def list_usage(self, tenant_id: str, *, since: str, until: str) -> list[UsageRecord]:
        """列出一段日期（两端都含）内有记录的用量，按日期排序。"""
        ...

    # --- 审计 ---

    async def append_audit(self, records: Sequence[AuditRecord]) -> int:
        """追加审计记录，返回实际写入的条数。

        ``(stream, seq)`` 相同的引擎事件只保留先到的；其余来源的记录不去重。
        """
        ...

    async def query_audit(self, tenant_id: str, query: AuditQuery) -> list[AuditRecord]:
        """查询审计记录，按标识升序。"""
        ...


__all__ = ["ControlPlaneStore"]
