"""控制面存储的 Postgres 实现：用于多实例部署。

租户隔离：除认证时按 ``key_id`` 查凭据外，每条语句都带 ``tenant_id`` 条件。
表名来自命名空间（字符集已校验），取值一律走参数绑定。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from xuanpu_server.controlplane.models import (
    AuditQuery,
    AuditRecord,
    PackageVersionRecord,
    SecretRecord,
    SessionRecord,
    UsageRecord,
)
from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE, ensure_current
from xuanpu_server.errors import ConflictError, NotFoundError, SessionBusyError
from xuanpu_server.tenancy import ApiKeyRecord, QuotaLimits, Tenant

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng_stores import SqlNamespace

_SESSION_COLUMNS = (
    "tenant_id, session_id, package, version, entry, thread_id, status, owner, "
    "lease_expires_at, created_at, updated_at, created_by"
)
_AUDIT_COLUMNS = (
    "audit_id, tenant_id, source, kind, at, actor, session_id, stream, seq, data::text AS data"
)


def _tenant(row: asyncpg.Record) -> Tenant:
    """行 → 租户。"""
    return Tenant(
        tenant_id=row["tenant_id"],
        name=row["name"],
        status=row["status"],
        quota=QuotaLimits(
            max_active_sessions=row["max_active_sessions"],
            max_daily_tokens=row["max_daily_tokens"],
        ),
        created_at=row["created_at"],
    )


def _api_key(row: asyncpg.Record) -> ApiKeyRecord:
    """行 → 凭据。"""
    return ApiKeyRecord(
        key_id=row["key_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        role=row["role"],
        secret_hash=row["secret_hash"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


def _package(row: asyncpg.Record) -> PackageVersionRecord:
    """行 → 业务包版本。"""
    return PackageVersionRecord(**dict(row))


def _session(row: asyncpg.Record) -> SessionRecord:
    """行 → 会话。"""
    return SessionRecord(**dict(row))


def _usage(row: asyncpg.Record) -> UsageRecord:
    """行 → 用量。"""
    return UsageRecord(**dict(row))


def _audit(row: asyncpg.Record) -> AuditRecord:
    """行 → 审计记录。"""
    fields = dict(row)
    fields["data"] = json.loads(fields["data"])
    return AuditRecord(**fields)


class PostgresControlPlane:
    """Postgres 里的控制面存储。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池。
            namespace: 表所在的 schema 与表名前缀。
        """
        self._pool = pool
        self._namespace = namespace
        self._t = {
            name: namespace.table(name)
            for name in (
                "tenant",
                "api_key",
                "package_version",
                "package_active",
                "secret",
                "session",
                "usage",
                "audit",
            )
        }

    async def check_schema(self) -> None:
        """核对表结构版本；落后时报错。"""
        await ensure_current(self._pool, self._namespace)

    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        """取多行。"""
        async with self._pool.acquire() as conn:
            return list(await conn.fetch(sql, *args))

    async def _fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        """取一行。"""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def _execute(self, sql: str, *args: Any) -> int:
        """执行并返回受影响的行数。"""
        async with self._pool.acquire() as conn:
            status = await conn.execute(sql, *args)
        return int(status.rsplit(" ", 1)[-1])

    # --- 租户与凭据 ---

    async def create_tenant(self, tenant: Tenant) -> None:
        """新建租户。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['tenant']} (tenant_id, name, status, "
                "max_active_sessions, max_daily_tokens, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                tenant.tenant_id,
                tenant.name,
                tenant.status,
                tenant.quota.max_active_sessions,
                tenant.quota.max_daily_tokens,
                tenant.created_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError(f"租户 {tenant.tenant_id!r} 已存在") from exc

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        """按标识取租户。"""
        row = await self._fetchrow(
            f"SELECT * FROM {self._t['tenant']} WHERE tenant_id = $1", tenant_id
        )
        return _tenant(row) if row is not None else None

    async def list_tenants(self) -> list[Tenant]:
        """列出全部租户。"""
        rows = await self._fetch(f"SELECT * FROM {self._t['tenant']} ORDER BY tenant_id")
        return [_tenant(row) for row in rows]

    async def update_tenant(self, tenant: Tenant) -> None:
        """整体替换租户记录。"""
        changed = await self._execute(
            f"UPDATE {self._t['tenant']} SET name = $2, status = $3, "
            "max_active_sessions = $4, max_daily_tokens = $5 WHERE tenant_id = $1",
            tenant.tenant_id,
            tenant.name,
            tenant.status,
            tenant.quota.max_active_sessions,
            tenant.quota.max_daily_tokens,
        )
        if not changed:
            raise NotFoundError(f"租户 {tenant.tenant_id!r} 不存在")

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        """登记一把 API key。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['api_key']} (key_id, tenant_id, name, role, "
                "secret_hash, created_at, revoked_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                record.key_id,
                record.tenant_id,
                record.name,
                record.role,
                record.secret_hash,
                record.created_at,
                record.revoked_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("API key 标识冲突") from exc

    async def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        """按标识取 API key。"""
        row = await self._fetchrow(f"SELECT * FROM {self._t['api_key']} WHERE key_id = $1", key_id)
        return _api_key(row) if row is not None else None

    async def list_api_keys(self, tenant_id: str) -> list[ApiKeyRecord]:
        """列出租户的 API key。"""
        rows = await self._fetch(
            f"SELECT * FROM {self._t['api_key']} WHERE tenant_id = $1 ORDER BY created_at, key_id",
            tenant_id,
        )
        return [_api_key(row) for row in rows]

    async def revoke_api_key(self, tenant_id: str, key_id: str, *, at: float) -> bool:
        """吊销 API key。"""
        changed = await self._execute(
            f"UPDATE {self._t['api_key']} SET revoked_at = $3 "
            "WHERE tenant_id = $1 AND key_id = $2 AND revoked_at IS NULL",
            tenant_id,
            key_id,
            at,
        )
        return bool(changed)

    # --- 业务包 ---

    async def add_package_version(self, record: PackageVersionRecord) -> None:
        """登记一个已发布版本。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['package_version']} (tenant_id, name, version, digest, "
                "manifest_yaml, location, published_at, published_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                record.tenant_id,
                record.name,
                record.version,
                record.digest,
                record.manifest_yaml,
                record.location,
                record.published_at,
                record.published_by,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError(f"业务包 {record.name} 的版本 {record.version} 已发布") from exc

    async def get_package_version(
        self, tenant_id: str, name: str, version: str
    ) -> PackageVersionRecord | None:
        """取某个版本。"""
        row = await self._fetchrow(
            f"SELECT * FROM {self._t['package_version']} "
            "WHERE tenant_id = $1 AND name = $2 AND version = $3",
            tenant_id,
            name,
            version,
        )
        return _package(row) if row is not None else None

    async def list_package_versions(
        self, tenant_id: str, name: str | None = None
    ) -> list[PackageVersionRecord]:
        """列出已发布版本。"""
        rows = await self._fetch(
            f"SELECT * FROM {self._t['package_version']} "
            "WHERE tenant_id = $1 AND ($2::text IS NULL OR name = $2) "
            "ORDER BY published_at, name, version",
            tenant_id,
            name,
        )
        return [_package(row) for row in rows]

    async def set_active_version(
        self, tenant_id: str, name: str, version: str, *, at: float
    ) -> None:
        """设置生效版本。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['package_active']} (tenant_id, name, version, activated_at) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (tenant_id, name) DO UPDATE "
                "SET version = EXCLUDED.version, activated_at = EXCLUDED.activated_at",
                tenant_id,
                name,
                version,
                at,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise NotFoundError(f"业务包 {name} 没有版本 {version}") from exc

    async def get_active_version(self, tenant_id: str, name: str) -> str | None:
        """取生效版本号。"""
        row = await self._fetchrow(
            f"SELECT version FROM {self._t['package_active']} WHERE tenant_id = $1 AND name = $2",
            tenant_id,
            name,
        )
        return str(row["version"]) if row is not None else None

    # --- 密钥 ---

    async def put_secret(self, record: SecretRecord) -> None:
        """写入或覆盖一条密钥。"""
        await self._execute(
            f"INSERT INTO {self._t['secret']} (tenant_id, name, ciphertext, updated_at, "
            "updated_by) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (tenant_id, name) DO UPDATE "
            "SET ciphertext = EXCLUDED.ciphertext, updated_at = EXCLUDED.updated_at, "
            "updated_by = EXCLUDED.updated_by",
            record.tenant_id,
            record.name,
            record.ciphertext,
            record.updated_at,
            record.updated_by,
        )

    async def get_secret(self, tenant_id: str, name: str) -> SecretRecord | None:
        """取一条密钥。"""
        row = await self._fetchrow(
            f"SELECT * FROM {self._t['secret']} WHERE tenant_id = $1 AND name = $2",
            tenant_id,
            name,
        )
        if row is None:
            return None
        fields = dict(row)
        fields["ciphertext"] = bytes(fields["ciphertext"])
        return SecretRecord(**fields)

    async def list_secret_names(self, tenant_id: str) -> list[str]:
        """列出密钥名。"""
        rows = await self._fetch(
            f"SELECT name FROM {self._t['secret']} WHERE tenant_id = $1 ORDER BY name", tenant_id
        )
        return [str(row["name"]) for row in rows]

    async def delete_secret(self, tenant_id: str, name: str) -> bool:
        """删除一条密钥。"""
        changed = await self._execute(
            f"DELETE FROM {self._t['secret']} WHERE tenant_id = $1 AND name = $2",
            tenant_id,
            name,
        )
        return bool(changed)

    # --- 会话 ---

    async def create_session(self, record: SessionRecord) -> None:
        """新建会话。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['session']} ({_SESSION_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                record.tenant_id,
                record.session_id,
                record.package,
                record.version,
                record.entry,
                record.thread_id,
                record.status,
                record.owner,
                record.lease_expires_at,
                record.created_at,
                record.updated_at,
                record.created_by,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("会话标识冲突") from exc

    async def get_session(self, tenant_id: str, session_id: str) -> SessionRecord | None:
        """取会话。"""
        row = await self._fetchrow(
            f"SELECT {_SESSION_COLUMNS} FROM {self._t['session']} "
            "WHERE tenant_id = $1 AND session_id = $2",
            tenant_id,
            session_id,
        )
        return _session(row) if row is not None else None

    async def list_sessions(
        self, tenant_id: str, *, status: str | None = None, limit: int = 50
    ) -> list[SessionRecord]:
        """列出会话。"""
        rows = await self._fetch(
            f"SELECT {_SESSION_COLUMNS} FROM {self._t['session']} "
            "WHERE tenant_id = $1 AND ($2::text IS NULL OR status = $2) "
            "ORDER BY updated_at DESC, session_id DESC LIMIT $3",
            tenant_id,
            status,
            limit,
        )
        return [_session(row) for row in rows]

    async def count_active_sessions(self, tenant_id: str) -> int:
        """租户当前的活动会话数。"""
        row = await self._fetchrow(
            f"SELECT count(*) AS n FROM {self._t['session']} "
            "WHERE tenant_id = $1 AND status = 'active'",
            tenant_id,
        )
        assert row is not None
        return int(row["n"])

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
        row = await self._fetchrow(
            f"UPDATE {self._t['session']} SET updated_at = $3, "
            "thread_id = COALESCE($4, thread_id), status = COALESCE($5, status) "
            f"WHERE tenant_id = $1 AND session_id = $2 RETURNING {_SESSION_COLUMNS}",
            tenant_id,
            session_id,
            at,
            thread_id,
            status,
        )
        if row is None:
            raise NotFoundError(f"会话 {session_id!r} 不存在")
        return _session(row)

    async def acquire_lease(
        self, tenant_id: str, session_id: str, *, owner: str, now: float, ttl_seconds: float
    ) -> SessionRecord:
        """取得或续期持有权。判断与写入在一条语句里完成，天然原子。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE {self._t['session']} SET owner = $3, lease_expires_at = $5 "
                "WHERE tenant_id = $1 AND session_id = $2 "
                "AND (owner IS NULL OR owner = $3 OR lease_expires_at <= $4) "
                f"RETURNING {_SESSION_COLUMNS}",
                tenant_id,
                session_id,
                owner,
                now,
                now + ttl_seconds,
            )
            if row is not None:
                return _session(row)
            holder = await conn.fetchrow(
                f"SELECT owner FROM {self._t['session']} WHERE tenant_id = $1 AND session_id = $2",
                tenant_id,
                session_id,
            )
        if holder is None:
            raise NotFoundError(f"会话 {session_id!r} 不存在")
        raise SessionBusyError(
            f"会话 {session_id!r} 正由另一个实例持有", owner=str(holder["owner"])
        )

    async def release_lease(self, tenant_id: str, session_id: str, *, owner: str) -> bool:
        """放弃持有权。"""
        changed = await self._execute(
            f"UPDATE {self._t['session']} SET owner = NULL, lease_expires_at = 0 "
            "WHERE tenant_id = $1 AND session_id = $2 AND owner = $3",
            tenant_id,
            session_id,
            owner,
        )
        return bool(changed)

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
        table = self._t["usage"]
        row = await self._fetchrow(
            f"INSERT INTO {table} AS u (tenant_id, day, input_tokens, output_tokens, "
            "total_tokens, turns) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (tenant_id, day) DO UPDATE SET "
            "input_tokens = u.input_tokens + EXCLUDED.input_tokens, "
            "output_tokens = u.output_tokens + EXCLUDED.output_tokens, "
            "total_tokens = u.total_tokens + EXCLUDED.total_tokens, "
            "turns = u.turns + EXCLUDED.turns RETURNING *",
            tenant_id,
            day,
            input_tokens,
            output_tokens,
            total_tokens,
            turns,
        )
        assert row is not None
        return _usage(row)

    async def get_usage(self, tenant_id: str, day: str) -> UsageRecord:
        """取某一天的用量。"""
        row = await self._fetchrow(
            f"SELECT * FROM {self._t['usage']} WHERE tenant_id = $1 AND day = $2", tenant_id, day
        )
        return _usage(row) if row is not None else UsageRecord(tenant_id, day)

    async def list_usage(self, tenant_id: str, *, since: str, until: str) -> list[UsageRecord]:
        """列出一段日期内的用量。"""
        rows = await self._fetch(
            f"SELECT * FROM {self._t['usage']} WHERE tenant_id = $1 AND day >= $2 AND day <= $3 "
            "ORDER BY day",
            tenant_id,
            since,
            until,
        )
        return [_usage(row) for row in rows]

    # --- 审计 ---

    async def append_audit(self, records: Sequence[AuditRecord]) -> int:
        """追加审计记录。"""
        if not records:
            return 0
        statement = (
            f"INSERT INTO {self._t['audit']} (tenant_id, source, kind, at, actor, session_id, "
            "stream, seq, data) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::text::jsonb) "
            "ON CONFLICT (tenant_id, stream, seq) DO NOTHING"
        )
        written = 0
        async with self._pool.acquire() as conn, conn.transaction():
            for record in records:
                status = await conn.execute(
                    statement,
                    record.tenant_id,
                    record.source,
                    record.kind,
                    record.at,
                    record.actor,
                    record.session_id,
                    record.stream,
                    record.seq,
                    json.dumps(record.data, ensure_ascii=False, default=str),
                )
                written += int(status.rsplit(" ", 1)[-1])
        return written

    async def query_audit(self, tenant_id: str, query: AuditQuery) -> list[AuditRecord]:
        """查询审计记录。"""
        rows = await self._fetch(
            f"SELECT {_AUDIT_COLUMNS} FROM {self._t['audit']} "
            "WHERE tenant_id = $1 AND audit_id > $2 "
            "AND ($3::text IS NULL OR session_id = $3) "
            "AND ($4::text IS NULL OR kind = $4) "
            "AND ($5::text IS NULL OR source = $5) "
            "AND ($6::float8 IS NULL OR at >= $6) "
            "AND ($7::float8 IS NULL OR at < $7) "
            "AND ($9::text IS NULL OR data->>'submission_id' = $9) "
            "ORDER BY audit_id LIMIT $8",
            tenant_id,
            query.after_id,
            query.session_id,
            query.kind,
            query.source,
            query.since,
            query.until,
            query.limit,
            query.submission_id,
        )
        return [_audit(row) for row in rows]


__all__ = ["PostgresControlPlane"]
