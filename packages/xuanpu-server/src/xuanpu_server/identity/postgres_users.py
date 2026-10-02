"""身份存储 Postgres 实现的基础部分：连接池与表名、语句助手，以及用户与外部身份两张表的读写。

由 ``identity.postgres.PostgresIdentityStore`` 继承，补上其余几张表，合成完整的
``IdentityStore``。拆开只为控制单个文件的行数；约定与 ``identity.postgres`` 的模块说明相同：
每条语句都带 ``tenant_id`` 条件，表名来自命名空间（字符集已校验），取值一律走参数绑定，按标识
排序与翻页用 ``COLLATE "C"``（按码点比较，与内存实现一致）。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import asyncpg

from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.identity.models import (
    ExternalIdentity,
    UserRecord,
    require_identity_of,
    require_page_size,
    require_status_change,
)

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy
    from taifeng_stores import SqlNamespace

    from xuanpu_server.identity.models import (
        DisabledReason,
        IdentityProvider,
        UserQuery,
        UserRole,
        UserStatus,
    )

_TABLES = (
    "user",
    "external_identity",
    "identity_config",
    "login_session",
    "login_attempt",
    "user_usage",
    "identity_reconcile",
)
_USER_INSERT_COLUMNS = (
    "tenant_id, user_id, display_name, created_at, role, avatar_url, status, disabled_reason, "
    "last_login_at, disabled_at, unverified_since"
)
_IDENTITY_COLUMNS = "tenant_id, provider, subject, user_id, attributes::text AS attributes"


def _user(row: asyncpg.Record) -> UserRecord:
    """行 → 用户。"""
    return UserRecord(**dict(row))


def _identity(row: asyncpg.Record) -> ExternalIdentity:
    """行 → 外部身份。"""
    fields = dict(row)
    fields["attributes"] = json.loads(fields["attributes"])
    return ExternalIdentity(**fields)


class PostgresUserTables:
    """Postgres 里的用户与外部身份；也持有身份域全部表的表名与语句助手。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池（与控制面共用）。
            namespace: 表所在的 schema 与表名前缀（与控制面相同）。
        """
        self._pool = pool
        self._t = {name: namespace.table(name) for name in _TABLES}

    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        """取多行。"""
        async with self._pool.acquire() as conn:
            return list(await conn.fetch(sql, *args))

    async def _fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        """取一行。"""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def _execute(self, sql: str, *args: Any) -> int:
        """执行并返回受影响的行数（取自命令状态，如 ``UPDATE 1``）。"""
        async with self._pool.acquire() as conn:
            status = await conn.execute(sql, *args)
        return int(status.rsplit(" ", 1)[-1])

    # --- 用户 ---

    async def create_user(
        self, user: UserRecord, *, identity: ExternalIdentity | None = None
    ) -> None:
        """新建用户，可一并建外部身份；两条插入在一个事务里。"""
        if identity is not None:
            require_identity_of(user, identity)
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    f"INSERT INTO {self._t['user']} ({_USER_INSERT_COLUMNS}) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    user.tenant_id,
                    user.user_id,
                    user.display_name,
                    user.created_at,
                    user.role,
                    user.avatar_url,
                    user.status,
                    user.disabled_reason,
                    user.last_login_at,
                    user.disabled_at,
                    user.unverified_since,
                )
                if identity is not None:
                    await self._insert_identity(conn, identity)
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("用户或外部身份已存在") from exc

    async def _insert_identity(
        self, conn: asyncpg.Connection | PoolConnectionProxy, identity: ExternalIdentity
    ) -> None:
        """插入一条外部身份（约束冲突由调用方转换）。"""
        await conn.execute(
            f"INSERT INTO {self._t['external_identity']} "
            "(tenant_id, provider, subject, user_id, attributes) "
            "VALUES ($1, $2, $3, $4, $5::text::jsonb)",
            identity.tenant_id,
            identity.provider,
            identity.subject,
            identity.user_id,
            json.dumps(identity.attributes, ensure_ascii=False),
        )

    async def get_user(self, tenant_id: str, user_id: str) -> UserRecord | None:
        """按标识取用户。"""
        row = await self._fetchrow(
            f"SELECT * FROM {self._t['user']} WHERE tenant_id = $1 AND user_id = $2",
            tenant_id,
            user_id,
        )
        return _user(row) if row is not None else None

    async def get_user_by_identity(
        self, tenant_id: str, provider: IdentityProvider, subject: str
    ) -> UserRecord | None:
        """按外部身份取用户。"""
        row = await self._fetchrow(
            f"SELECT u.* FROM {self._t['user']} u JOIN {self._t['external_identity']} e "
            "ON e.tenant_id = u.tenant_id AND e.user_id = u.user_id "
            "WHERE e.tenant_id = $1 AND e.provider = $2 AND e.subject = $3",
            tenant_id,
            provider,
            subject,
        )
        return _user(row) if row is not None else None

    async def list_users(self, tenant_id: str, query: UserQuery) -> list[UserRecord]:
        """按条件列出用户。"""
        rows = await self._fetch(
            f"SELECT * FROM {self._t['user']} WHERE tenant_id = $1 "
            "AND ($2::text IS NULL OR status = $2) "
            "AND ($3::text IS NULL OR role = $3) "
            "AND ($4::boolean IS NULL OR (unverified_since IS NOT NULL) = $4) "
            'AND ($5::text IS NULL OR user_id COLLATE "C" > $5) '
            'ORDER BY user_id COLLATE "C" LIMIT $6',
            tenant_id,
            query.status,
            query.role,
            query.unverified,
            query.after,
            query.limit,
        )
        return [_user(row) for row in rows]

    async def _require_user_exists(self, tenant_id: str, user_id: str) -> None:
        """用户不存在则报错。用户不会被删，所以「先改、没改到再查」不会误判。"""
        if await self.get_user(tenant_id, user_id) is None:
            raise NotFoundError(f"用户 {user_id!r} 不存在")

    async def _update_user(
        self, assignments: str, tenant_id: str, user_id: str, *args: Any
    ) -> UserRecord:
        """按 ``assignments``（``$3`` 起依次是 ``args``）改一个用户，返回改后的记录。

        ``assignments`` 只来自本类里写死的字面量，不含外部输入。
        """
        row = await self._fetchrow(
            f"UPDATE {self._t['user']} SET {assignments} "
            "WHERE tenant_id = $1 AND user_id = $2 RETURNING *",
            tenant_id,
            user_id,
            *args,
        )
        if row is None:
            raise NotFoundError(f"用户 {user_id!r} 不存在")
        return _user(row)

    async def set_user_role(self, tenant_id: str, user_id: str, role: UserRole) -> UserRecord:
        """改角色。"""
        return await self._update_user("role = $3", tenant_id, user_id, role)

    async def set_user_status(
        self,
        tenant_id: str,
        user_id: str,
        status: UserStatus,
        *,
        reason: DisabledReason | None = None,
        at: float,
    ) -> bool:
        """改状态；``status <> $3`` 的条件让并发的同向修改只有一个生效。"""
        require_status_change(status, reason)
        changed = await self._execute(
            f"UPDATE {self._t['user']} SET status = $3, disabled_reason = $4, disabled_at = $5 "
            "WHERE tenant_id = $1 AND user_id = $2 AND status <> $3",
            tenant_id,
            user_id,
            status,
            reason,
            at if status == "disabled" else None,
        )
        if not changed:
            await self._require_user_exists(tenant_id, user_id)
        return bool(changed)

    async def update_user_profile(
        self,
        tenant_id: str,
        user_id: str,
        *,
        display_name: str,
        avatar_url: str | None,
        last_login_at: float | None = None,
    ) -> UserRecord:
        """刷新显示名与头像，可选地更新登录时间。"""
        return await self._update_user(
            "display_name = $3, avatar_url = $4, "
            "last_login_at = COALESCE($5::float8, last_login_at)",
            tenant_id,
            user_id,
            display_name,
            avatar_url,
            last_login_at,
        )

    async def mark_user_unverified(self, tenant_id: str, user_id: str, *, at: float) -> bool:
        """标记查不到；已有起始时间的不动。"""
        changed = await self._execute(
            f"UPDATE {self._t['user']} SET unverified_since = $3 "
            "WHERE tenant_id = $1 AND user_id = $2 AND unverified_since IS NULL",
            tenant_id,
            user_id,
            at,
        )
        if not changed:
            await self._require_user_exists(tenant_id, user_id)
        return bool(changed)

    async def clear_user_unverified(self, tenant_id: str, user_id: str) -> bool:
        """清掉查不到标记。"""
        changed = await self._execute(
            f"UPDATE {self._t['user']} SET unverified_since = NULL "
            "WHERE tenant_id = $1 AND user_id = $2 AND unverified_since IS NOT NULL",
            tenant_id,
            user_id,
        )
        if not changed:
            await self._require_user_exists(tenant_id, user_id)
        return bool(changed)

    # --- 外部身份 ---

    async def create_external_identity(self, identity: ExternalIdentity) -> None:
        """给已有用户登记外部身份。"""
        try:
            async with self._pool.acquire() as conn:
                await self._insert_identity(conn, identity)
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("外部身份已登记") from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise NotFoundError(f"用户 {identity.user_id!r} 不存在") from exc

    async def list_external_identities(
        self,
        tenant_id: str,
        provider: IdentityProvider,
        *,
        user_id: str | None = None,
        user_status: UserStatus | None = None,
        after_subject: str | None = None,
        limit: int = 100,
    ) -> list[ExternalIdentity]:
        """列出某身份源下的外部身份。"""
        require_page_size(limit)
        rows = await self._fetch(
            "SELECT e.tenant_id, e.provider, e.subject, e.user_id, "
            "e.attributes::text AS attributes "
            f"FROM {self._t['external_identity']} e JOIN {self._t['user']} u "
            "ON u.tenant_id = e.tenant_id AND u.user_id = e.user_id "
            "WHERE e.tenant_id = $1 AND e.provider = $2 "
            "AND ($3::text IS NULL OR e.user_id = $3) "
            "AND ($4::text IS NULL OR u.status = $4) "
            'AND ($5::text IS NULL OR e.subject COLLATE "C" > $5) '
            'ORDER BY e.subject COLLATE "C" LIMIT $6',
            tenant_id,
            provider,
            user_id,
            user_status,
            after_subject,
            limit,
        )
        return [_identity(row) for row in rows]

    async def update_identity_attributes(
        self, tenant_id: str, provider: IdentityProvider, subject: str, attributes: dict[str, str]
    ) -> ExternalIdentity:
        """整体替换外部身份的属性。"""
        row = await self._fetchrow(
            f"UPDATE {self._t['external_identity']} SET attributes = $4::text::jsonb "
            "WHERE tenant_id = $1 AND provider = $2 AND subject = $3 "
            f"RETURNING {_IDENTITY_COLUMNS}",
            tenant_id,
            provider,
            subject,
            json.dumps(attributes, ensure_ascii=False),
        )
        if row is None:
            raise NotFoundError("外部身份不存在")
        return _identity(row)


__all__ = ["PostgresUserTables"]
