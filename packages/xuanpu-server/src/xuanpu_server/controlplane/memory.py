"""控制面存储的内存实现：用于测试与单机试用。进程退出即丢失。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from xuanpu_server.controlplane.models import (
    AuditQuery,
    AuditRecord,
    PackageVersionRecord,
    SecretRecord,
    SessionRecord,
    UsageRecord,
)
from xuanpu_server.errors import ConflictError, NotFoundError, SessionBusyError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from xuanpu_server.tenancy import ApiKeyRecord, Tenant


class InMemoryControlPlane:
    """内存里的控制面存储。

    所有写操作在一把锁里完成；方法内部没有让出点，所以读操作不需要加锁。
    """

    def __init__(self) -> None:
        """从空状态开始。"""
        self._lock = asyncio.Lock()
        self._tenants: dict[str, Tenant] = {}
        self._keys: dict[str, ApiKeyRecord] = {}
        self._packages: dict[tuple[str, str, str], PackageVersionRecord] = {}
        self._active: dict[tuple[str, str], str] = {}
        self._secrets: dict[tuple[str, str], SecretRecord] = {}
        self._sessions: dict[tuple[str, str], SessionRecord] = {}
        self._usage: dict[tuple[str, str], UsageRecord] = {}
        self._audit: list[AuditRecord] = []
        self._audit_keys: set[tuple[str, str, int]] = set()

    # --- 租户与凭据 ---

    async def create_tenant(self, tenant: Tenant) -> None:
        """新建租户。"""
        async with self._lock:
            if tenant.tenant_id in self._tenants:
                raise ConflictError(f"租户 {tenant.tenant_id!r} 已存在")
            self._tenants[tenant.tenant_id] = tenant

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        """按标识取租户。"""
        return self._tenants.get(tenant_id)

    async def list_tenants(self) -> list[Tenant]:
        """列出全部租户。"""
        return sorted(self._tenants.values(), key=lambda tenant: tenant.tenant_id)

    async def update_tenant(self, tenant: Tenant) -> None:
        """整体替换租户记录。"""
        async with self._lock:
            if tenant.tenant_id not in self._tenants:
                raise NotFoundError(f"租户 {tenant.tenant_id!r} 不存在")
            self._tenants[tenant.tenant_id] = tenant

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        """登记一把 API key。"""
        async with self._lock:
            if record.key_id in self._keys:
                raise ConflictError("API key 标识冲突")
            self._keys[record.key_id] = record

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        """按标识取 API key。"""
        return self._keys.get(key_id)

    async def list_api_keys(self, tenant_id: str) -> list[ApiKeyRecord]:
        """列出租户的 API key。"""
        found = [record for record in self._keys.values() if record.tenant_id == tenant_id]
        return sorted(found, key=lambda record: (record.created_at, record.key_id))

    async def revoke_api_key(self, tenant_id: str, key_id: str, *, at: float) -> bool:
        """吊销 API key。"""
        async with self._lock:
            record = self._keys.get(key_id)
            if record is None or record.tenant_id != tenant_id or not record.active:
                return False
            self._keys[key_id] = replace(record, revoked_at=at)
            return True

    # --- 业务包 ---

    async def add_package_version(self, record: PackageVersionRecord) -> None:
        """登记一个已发布版本。"""
        key = (record.tenant_id, record.name, record.version)
        async with self._lock:
            if key in self._packages:
                raise ConflictError(f"业务包 {record.name} 的版本 {record.version} 已发布")
            self._packages[key] = record

    async def get_package_version(
        self, tenant_id: str, name: str, version: str
    ) -> PackageVersionRecord | None:
        """取某个版本。"""
        return self._packages.get((tenant_id, name, version))

    async def list_package_versions(
        self, tenant_id: str, name: str | None = None
    ) -> list[PackageVersionRecord]:
        """列出已发布版本。"""
        found = [
            record
            for record in self._packages.values()
            if record.tenant_id == tenant_id and (name is None or record.name == name)
        ]
        return sorted(found, key=lambda record: (record.published_at, record.name, record.version))

    async def set_active_version(
        self, tenant_id: str, name: str, version: str, *, at: float
    ) -> None:
        """设置生效版本。"""
        async with self._lock:
            if (tenant_id, name, version) not in self._packages:
                raise NotFoundError(f"业务包 {name} 没有版本 {version}")
            self._active[(tenant_id, name)] = version

    async def get_active_version(self, tenant_id: str, name: str) -> str | None:
        """取生效版本号。"""
        return self._active.get((tenant_id, name))

    # --- 密钥 ---

    async def put_secret(self, record: SecretRecord) -> None:
        """写入或覆盖一条密钥。"""
        async with self._lock:
            self._secrets[(record.tenant_id, record.name)] = record

    async def get_secret(self, tenant_id: str, name: str) -> SecretRecord | None:
        """取一条密钥。"""
        return self._secrets.get((tenant_id, name))

    async def list_secret_names(self, tenant_id: str) -> list[str]:
        """列出密钥名。"""
        return sorted(name for owner, name in self._secrets if owner == tenant_id)

    async def delete_secret(self, tenant_id: str, name: str) -> bool:
        """删除一条密钥。"""
        async with self._lock:
            return self._secrets.pop((tenant_id, name), None) is not None

    # --- 会话 ---

    async def create_session(self, record: SessionRecord) -> None:
        """新建会话。"""
        key = (record.tenant_id, record.session_id)
        async with self._lock:
            if key in self._sessions:
                raise ConflictError("会话标识冲突")
            self._sessions[key] = record

    async def get_session(self, tenant_id: str, session_id: str) -> SessionRecord | None:
        """取会话。"""
        return self._sessions.get((tenant_id, session_id))

    async def list_sessions(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
    ) -> list[SessionRecord]:
        """列出会话。"""
        found = [
            record
            for record in self._sessions.values()
            if record.tenant_id == tenant_id
            and (status is None or record.status == status)
            and (created_by is None or record.created_by == created_by)
        ]
        found.sort(key=lambda record: (record.updated_at, record.session_id), reverse=True)
        return found[:limit]

    async def count_active_sessions(self, tenant_id: str) -> int:
        """租户当前的活动会话数。"""
        return sum(
            1
            for record in self._sessions.values()
            if record.tenant_id == tenant_id and record.status == "active"
        )

    def _require_session(self, tenant_id: str, session_id: str) -> SessionRecord:
        """取会话，不存在则报错。调用方须持有锁。"""
        record = self._sessions.get((tenant_id, session_id))
        if record is None:
            raise NotFoundError(f"会话 {session_id!r} 不存在")
        return record

    async def update_session(
        self,
        tenant_id: str,
        session_id: str,
        *,
        at: float,
        thread_id: str | None = None,
        status: str | None = None,
    ) -> SessionRecord:
        """更新会话。"""
        async with self._lock:
            record = self._require_session(tenant_id, session_id)
            updated = replace(
                record,
                updated_at=at,
                thread_id=thread_id if thread_id is not None else record.thread_id,
                status=status if status is not None else record.status,  # type: ignore[arg-type]
            )
            self._sessions[(tenant_id, session_id)] = updated
            return updated

    async def acquire_lease(
        self, tenant_id: str, session_id: str, *, owner: str, now: float, ttl_seconds: float
    ) -> SessionRecord:
        """取得或续期持有权。"""
        async with self._lock:
            record = self._require_session(tenant_id, session_id)
            held_by_other = record.owner not in (None, owner) and record.lease_expires_at > now
            if held_by_other:
                assert record.owner is not None
                raise SessionBusyError(
                    f"会话 {session_id!r} 正由另一个实例持有", owner=record.owner
                )
            updated = replace(record, owner=owner, lease_expires_at=now + ttl_seconds)
            self._sessions[(tenant_id, session_id)] = updated
            return updated

    async def release_lease(self, tenant_id: str, session_id: str, *, owner: str) -> bool:
        """放弃持有权。"""
        async with self._lock:
            record = self._sessions.get((tenant_id, session_id))
            if record is None or record.owner != owner:
                return False
            self._sessions[(tenant_id, session_id)] = replace(
                record, owner=None, lease_expires_at=0.0
            )
            return True

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
        """累加用量。"""
        async with self._lock:
            current = self._usage.get((tenant_id, day), UsageRecord(tenant_id, day))
            updated = UsageRecord(
                tenant_id=tenant_id,
                day=day,
                input_tokens=current.input_tokens + input_tokens,
                output_tokens=current.output_tokens + output_tokens,
                total_tokens=current.total_tokens + total_tokens,
                turns=current.turns + turns,
            )
            self._usage[(tenant_id, day)] = updated
            return updated

    async def get_usage(self, tenant_id: str, day: str) -> UsageRecord:
        """取某一天的用量。"""
        return self._usage.get((tenant_id, day), UsageRecord(tenant_id, day))

    async def list_usage(self, tenant_id: str, *, since: str, until: str) -> list[UsageRecord]:
        """列出一段日期内的用量。"""
        found = [
            record
            for (owner, day), record in self._usage.items()
            if owner == tenant_id and since <= day <= until
        ]
        return sorted(found, key=lambda record: record.day)

    # --- 审计 ---

    async def append_audit(self, records: Sequence[AuditRecord]) -> int:
        """追加审计记录。"""
        written = 0
        async with self._lock:
            for record in records:
                if record.stream is not None and record.seq is not None:
                    key = (record.tenant_id, record.stream, record.seq)
                    if key in self._audit_keys:
                        continue
                    self._audit_keys.add(key)
                self._audit.append(replace(record, audit_id=len(self._audit) + 1))
                written += 1
        return written

    async def query_audit(self, tenant_id: str, query: AuditQuery) -> list[AuditRecord]:
        """查询审计记录。"""
        found = [
            record
            for record in self._audit
            if record.tenant_id == tenant_id
            and record.audit_id > query.after_id
            and query.matches(record)
        ]
        return found[: query.limit]


__all__ = ["InMemoryControlPlane"]
