"""Google 通讯录关联仓储：内存与 PostgreSQL 双后端，托管 OAuth 尝试、密文凭据与同步映射。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from server.core.controlplane.models import AuditRecord
from server.core.controlplane.schema import DEFAULT_NAMESPACE

if TYPE_CHECKING:
    from collections.abc import Callable

    import asyncpg
    from taifeng_stores import SqlNamespace


@dataclass(slots=True)
class GoogleAttempt:
    """一次性 OAuth + PKCE 授权尝试记录（有效期 5 分钟）。"""

    state_hash: str
    tenant_id: str
    user_id: str
    cookie_hash: str
    code_verifier: str
    return_to: str
    expires_at: float


@dataclass(slots=True)
class GoogleAccountRecord:
    """员工本人的 Google 关联账号与加密令牌元数据（不含明文 token）。"""

    tenant_id: str
    user_id: str
    google_sub: str
    email: str
    display_name: str
    scopes: str
    token_revision: str
    access_token_cipher: bytes
    refresh_token_cipher: bytes | None
    access_expires_at: float
    sync_token: str | None
    linked_at: float
    last_synced_at: float | None
    updated_at: float


@dataclass(slots=True)
class GoogleContactLink:
    """Google People resourceName 与本地个人通讯录 person_id 的映射。"""

    resource_name: str
    person_id: str
    etag: str
    synced_at: float


class GoogleContactsStore(Protocol):
    """Google 通讯录关联存储契约，严格按 (tenant_id, user_id) 隔离。"""

    async def put_attempt(self, attempt: GoogleAttempt, *, now: float) -> None:
        """写入一次性授权尝试并清理过期尝试。"""
        ...

    async def take_attempt(self, state_hash: str, *, now: float) -> GoogleAttempt | None:
        """原子消费并删除未过期的授权尝试。"""
        ...

    async def get_account(self, tenant_id: str, user_id: str) -> GoogleAccountRecord | None:
        """读取本人已关联的 Google 账号记录。"""
        ...

    async def upsert_account(self, record: GoogleAccountRecord) -> None:
        """保存或更新本人 Google 关联账号与密文令牌。"""
        ...

    async def delete_account(self, tenant_id: str, user_id: str) -> bool:
        """解除关联：删除账号密文令牌及其全部外部联系人映射。"""
        ...

    async def list_links(self, tenant_id: str, user_id: str) -> dict[str, GoogleContactLink]:
        """读取本人全部 Google resourceName -> 本地 person_id 映射。"""
        ...

    async def upsert_links(
        self, tenant_id: str, user_id: str, links: list[GoogleContactLink]
    ) -> None:
        """批量保存或更新 Google 联系人映射。"""
        ...

    async def delete_links(
        self, tenant_id: str, user_id: str, resource_names: list[str]
    ) -> None:
        """删除已被 Google 侧删除的联系人映射。"""
        ...

    async def append_audit(
        self, tenant_id: str, user_id: str, action: str, data: dict[str, Any], at: float
    ) -> None:
        """写入脱敏审计记录。"""
        ...


class MemoryGoogleContactsStore:
    """内存后端实现，用于单机试用与自动化测试。"""

    def __init__(self, audit_sink: Callable[[list[AuditRecord]], None] | None = None) -> None:
        """复用控制面的个人空间脱敏审计出口。"""
        self._audit = audit_sink
        self._attempts: dict[str, GoogleAttempt] = {}
        self._accounts: dict[tuple[str, str], GoogleAccountRecord] = {}
        self._links: dict[tuple[str, str], dict[str, GoogleContactLink]] = {}

    async def put_attempt(self, attempt: GoogleAttempt, *, now: float) -> None:
        """保存尝试并顺便回收过期项。"""
        expired = [k for k, v in self._attempts.items() if v.expires_at <= now]
        for k in expired:
            del self._attempts[k]
        self._attempts[attempt.state_hash] = deepcopy(attempt)

    async def take_attempt(self, state_hash: str, *, now: float) -> GoogleAttempt | None:
        """一次性取出授权尝试；过期则丢弃。"""
        found = self._attempts.pop(state_hash, None)
        if found is None or found.expires_at <= now:
            return None
        return deepcopy(found)

    async def get_account(self, tenant_id: str, user_id: str) -> GoogleAccountRecord | None:
        """按本人读取 Google 关联记录。"""
        rec = self._accounts.get((tenant_id, user_id))
        return deepcopy(rec) if rec is not None else None

    async def upsert_account(self, record: GoogleAccountRecord) -> None:
        """保存或覆盖本人 Google 关联记录。"""
        self._accounts[(record.tenant_id, record.user_id)] = deepcopy(record)

    async def delete_account(self, tenant_id: str, user_id: str) -> bool:
        """清除本人关联账号与映射。"""
        existed = self._accounts.pop((tenant_id, user_id), None) is not None
        self._links.pop((tenant_id, user_id), None)
        return existed

    async def list_links(self, tenant_id: str, user_id: str) -> dict[str, GoogleContactLink]:
        """返回本人全部外部联系人映射副本。"""
        return deepcopy(self._links.get((tenant_id, user_id), {}))

    async def upsert_links(
        self, tenant_id: str, user_id: str, links: list[GoogleContactLink]
    ) -> None:
        """批量写入或更新联系人映射。"""
        bucket = self._links.setdefault((tenant_id, user_id), {})
        for link in links:
            bucket[link.resource_name] = deepcopy(link)

    async def delete_links(
        self, tenant_id: str, user_id: str, resource_names: list[str]
    ) -> None:
        """移除指定 resource_name 映射。"""
        bucket = self._links.get((tenant_id, user_id))
        if not bucket:
            return
        for rn in resource_names:
            bucket.pop(rn, None)

    async def append_audit(
        self, tenant_id: str, user_id: str, action: str, data: dict[str, Any], at: float
    ) -> None:
        """写入脱敏审计。"""
        if self._audit is not None:
            self._audit([
                AuditRecord(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source="control",
                    kind=action,
                    at=at,
                    actor=f"user:{user_id}",
                    data=deepcopy(data),
                )
            ])


class PostgresGoogleContactsStore:
    """PostgreSQL 后端实现，与控制面共用表命名空间。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名来自已校验的 SqlNamespace，业务字段全部参数绑定。"""
        self._pool = pool
        self._t = {
            name: namespace.table(name)
            for name in (
                "audit",
                "personal_google_attempt",
                "personal_google_account",
                "personal_google_contact_link",
            )
        }

    async def put_attempt(self, attempt: GoogleAttempt, *, now: float) -> None:
        """写入一次性授权尝试并清理过期行。"""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"DELETE FROM {self._t['personal_google_attempt']} WHERE expires_at <= $1",  # noqa: S608
                now,
            )
            await conn.execute(
                f"INSERT INTO {self._t['personal_google_attempt']} "  # noqa: S608
                "(state_hash, tenant_id, user_id, cookie_hash, code_verifier, "
                "return_to, expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                attempt.state_hash,
                attempt.tenant_id,
                attempt.user_id,
                attempt.cookie_hash,
                attempt.code_verifier,
                attempt.return_to,
                attempt.expires_at,
            )

    async def take_attempt(self, state_hash: str, *, now: float) -> GoogleAttempt | None:
        """原子删除并返回未过期的授权尝试。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM {self._t['personal_google_attempt']} "  # noqa: S608
                "WHERE state_hash = $1 AND expires_at > $2 "
                "RETURNING state_hash, tenant_id, user_id, cookie_hash, code_verifier, "
                "return_to, expires_at",
                state_hash,
                now,
            )
        if row is None:
            return None
        return GoogleAttempt(
            state_hash=row["state_hash"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            cookie_hash=row["cookie_hash"],
            code_verifier=row["code_verifier"],
            return_to=row["return_to"],
            expires_at=row["expires_at"],
        )

    async def get_account(self, tenant_id: str, user_id: str) -> GoogleAccountRecord | None:
        """读取本人 Google 关联账号。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT tenant_id, user_id, google_sub, email, display_name, scopes, "  # noqa: S608
                "token_revision, access_token_cipher, refresh_token_cipher, access_expires_at, "
                "sync_token, linked_at, last_synced_at, updated_at "
                f"FROM {self._t['personal_google_account']} "
                "WHERE tenant_id = $1 AND user_id = $2",
                tenant_id,
                user_id,
            )
        if row is None:
            return None
        return GoogleAccountRecord(
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            google_sub=row["google_sub"],
            email=row["email"],
            display_name=row["display_name"],
            scopes=row["scopes"],
            token_revision=row["token_revision"],
            access_token_cipher=bytes(row["access_token_cipher"]),
            refresh_token_cipher=(
                bytes(row["refresh_token_cipher"])
                if row["refresh_token_cipher"] is not None
                else None
            ),
            access_expires_at=row["access_expires_at"],
            sync_token=row["sync_token"],
            linked_at=row["linked_at"],
            last_synced_at=row["last_synced_at"],
            updated_at=row["updated_at"],
        )

    async def upsert_account(self, record: GoogleAccountRecord) -> None:
        """保存或更新本人 Google 关联账号与密文令牌。"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._t['personal_google_account']} "  # noqa: S608
                "(tenant_id, user_id, google_sub, email, display_name, scopes, token_revision, "
                "access_token_cipher, refresh_token_cipher, access_expires_at, sync_token, "
                "linked_at, last_synced_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
                "google_sub = EXCLUDED.google_sub, email = EXCLUDED.email, "
                "display_name = EXCLUDED.display_name, scopes = EXCLUDED.scopes, "
                "token_revision = EXCLUDED.token_revision, "
                "access_token_cipher = EXCLUDED.access_token_cipher, "
                "refresh_token_cipher = EXCLUDED.refresh_token_cipher, "
                "access_expires_at = EXCLUDED.access_expires_at, "
                "sync_token = EXCLUDED.sync_token, linked_at = EXCLUDED.linked_at, "
                "last_synced_at = EXCLUDED.last_synced_at, updated_at = EXCLUDED.updated_at",
                record.tenant_id,
                record.user_id,
                record.google_sub,
                record.email,
                record.display_name,
                record.scopes,
                record.token_revision,
                record.access_token_cipher,
                record.refresh_token_cipher,
                record.access_expires_at,
                record.sync_token,
                record.linked_at,
                record.last_synced_at,
                record.updated_at,
            )

    async def delete_account(self, tenant_id: str, user_id: str) -> bool:
        """原子解除本人关联账号与外部联系人映射。"""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"DELETE FROM {self._t['personal_google_contact_link']} "  # noqa: S608
                "WHERE tenant_id = $1 AND user_id = $2",
                tenant_id,
                user_id,
            )
            tag = await conn.execute(
                f"DELETE FROM {self._t['personal_google_account']} "  # noqa: S608
                "WHERE tenant_id = $1 AND user_id = $2",
                tenant_id,
                user_id,
            )
        return str(tag) != "DELETE 0"

    async def list_links(self, tenant_id: str, user_id: str) -> dict[str, GoogleContactLink]:
        """读取本人全部外部联系人映射。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT resource_name, person_id, etag, synced_at "  # noqa: S608
                f"FROM {self._t['personal_google_contact_link']} "
                "WHERE tenant_id = $1 AND user_id = $2",
                tenant_id,
                user_id,
            )
        return {
            row["resource_name"]: GoogleContactLink(
                resource_name=row["resource_name"],
                person_id=row["person_id"],
                etag=row["etag"],
                synced_at=row["synced_at"],
            )
            for row in rows
        }

    async def upsert_links(
        self, tenant_id: str, user_id: str, links: list[GoogleContactLink]
    ) -> None:
        """批量保存或更新联系人映射。"""
        if not links:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                f"INSERT INTO {self._t['personal_google_contact_link']} "  # noqa: S608
                "(tenant_id, user_id, resource_name, person_id, etag, synced_at) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (tenant_id, user_id, resource_name) DO UPDATE SET "
                "person_id = EXCLUDED.person_id, etag = EXCLUDED.etag, "
                "synced_at = EXCLUDED.synced_at",
                [
                    (tenant_id, user_id, lk.resource_name, lk.person_id, lk.etag, lk.synced_at)
                    for lk in links
                ],
            )

    async def delete_links(
        self, tenant_id: str, user_id: str, resource_names: list[str]
    ) -> None:
        """批量删除失效的外部联系人映射。"""
        if not resource_names:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {self._t['personal_google_contact_link']} "  # noqa: S608
                "WHERE tenant_id = $1 AND user_id = $2 AND resource_name = ANY($3::text[])",
                tenant_id,
                user_id,
                resource_names,
            )

    async def append_audit(
        self, tenant_id: str, user_id: str, action: str, data: dict[str, Any], at: float
    ) -> None:
        """写入脱敏审计记录。"""
        import json

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._t['audit']} "  # noqa: S608
                "(tenant_id, user_id, source, kind, at, actor, data) "
                "VALUES ($1, $2, 'control', $3, $4, $5, $6::jsonb)",
                tenant_id,
                user_id,
                action,
                at,
                f"user:{user_id}",
                json.dumps(data, ensure_ascii=False),
            )
