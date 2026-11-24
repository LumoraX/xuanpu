"""自有账号存储的 Postgres 实现，由 ``PostgresIdentityStore.credentials`` 提供。

与身份存储共用连接池与命名空间，表由控制面迁移 ``0026_local_credential`` 建出。约定同
``identity.postgres``：除 ``CredentialStore`` 说明里的两处例外，每条语句都带 ``tenant_id``；表名来自
命名空间，取值一律走参数绑定；读取写明列名。员工不存在时外键冲突转成 ``NotFoundError``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg

from server.core.errors import ConflictError, NotFoundError
from server.domains.identity.credential_models import AccountToken, LocalCredential
from server.domains.identity.postgres_users import affected

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy
    from taifeng_stores import SqlNamespace

    Connection = asyncpg.Connection | PoolConnectionProxy

_CREDENTIAL_COLUMNS = (
    "tenant_id, user_id, username, password_hash, created_at, password_changed_at, "
    "failed_attempts, locked_until"
)
# 列名，不是口令
_TOKEN_COLUMNS = (
    "token_hash, tenant_id, user_id, purpose, created_at, expires_at, issued_by"  # noqa: S105
)


def _credential(row: asyncpg.Record) -> LocalCredential:
    """行 → 凭据。"""
    return LocalCredential(**dict(row))


def _token(row: asyncpg.Record) -> AccountToken:
    """行 → 设置链接。"""
    return AccountToken(**dict(row))


class PostgresCredentialStore:
    """Postgres 里的凭据与设置链接。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池（与控制面共用）。
            namespace: 表所在的 schema 与表名前缀（与控制面相同）。
        """
        self._pool = pool
        self._credential = namespace.table("local_credential")
        self._token = namespace.table("account_token")

    async def _fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        """取一行。"""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def create_credential(self, credential: LocalCredential) -> None:
        """开通凭据；员工已有凭据与用户名被占用分别给出提示。"""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {self._credential} ({_CREDENTIAL_COLUMNS}) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    credential.tenant_id,
                    credential.user_id,
                    credential.username,
                    credential.password_hash,
                    credential.created_at,
                    credential.password_changed_at,
                    credential.failed_attempts,
                    credential.locked_until,
                )
        except asyncpg.UniqueViolationError as exc:
            if (exc.constraint_name or "").endswith("local_credential_username_key"):
                raise ConflictError(f"用户名 {credential.username!r} 已被占用") from exc
            raise ConflictError("这名员工已经开通了用户名口令") from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise NotFoundError(f"用户 {credential.user_id!r} 不存在") from exc

    async def get_credential(self, tenant_id: str, user_id: str) -> LocalCredential | None:
        """按员工取凭据。"""
        row = await self._fetchrow(
            f"SELECT {_CREDENTIAL_COLUMNS} FROM {self._credential} "
            "WHERE tenant_id = $1 AND user_id = $2",
            tenant_id,
            user_id,
        )
        return _credential(row) if row is not None else None

    async def find_credential(self, tenant_id: str, username: str) -> LocalCredential | None:
        """按用户名取凭据。"""
        row = await self._fetchrow(
            f"SELECT {_CREDENTIAL_COLUMNS} FROM {self._credential} "
            "WHERE tenant_id = $1 AND username = $2",
            tenant_id,
            username,
        )
        return _credential(row) if row is not None else None

    async def record_login_failure(
        self, tenant_id: str, user_id: str, *, now: float, lock_after: int, lock_seconds: float
    ) -> LocalCredential | None:
        """一条 UPDATE 里加计数、按阈值锁定，并发失败不会少记。"""
        row = await self._fetchrow(
            f"UPDATE {self._credential} SET "
            "locked_until = CASE WHEN failed_attempts + 1 >= $3 THEN $4::double precision "
            "ELSE locked_until END, "
            "failed_attempts = CASE WHEN failed_attempts + 1 >= $3 THEN 0 "
            "ELSE failed_attempts + 1 END "
            f"WHERE tenant_id = $1 AND user_id = $2 RETURNING {_CREDENTIAL_COLUMNS}",
            tenant_id,
            user_id,
            lock_after,
            now + lock_seconds,
        )
        return _credential(row) if row is not None else None

    async def record_login_success(
        self, tenant_id: str, user_id: str, *, rehashed: str | None = None
    ) -> None:
        """清零失败计数、解除锁定，按需换摘要。"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {self._credential} SET failed_attempts = 0, locked_until = NULL, "
                "password_hash = COALESCE($3, password_hash) "
                "WHERE tenant_id = $1 AND user_id = $2",
                tenant_id,
                user_id,
                rehashed,
            )

    async def _set_password(
        self, conn: Connection, tenant_id: str, user_id: str, password_hash: str, at: float
    ) -> bool:
        """事务内调用：写口令、清计数、删掉这名员工的设置链接。"""
        status = await conn.execute(
            f"UPDATE {self._credential} SET password_hash = $3, password_changed_at = $4, "
            "failed_attempts = 0, locked_until = NULL WHERE tenant_id = $1 AND user_id = $2",
            tenant_id,
            user_id,
            password_hash,
            at,
        )
        if affected(status) == 0:
            return False
        await conn.execute(
            f"DELETE FROM {self._token} WHERE tenant_id = $1 AND user_id = $2", tenant_id, user_id
        )
        return True

    async def set_password(
        self, tenant_id: str, user_id: str, password_hash: str, *, at: float
    ) -> bool:
        """设口令。"""
        async with self._pool.acquire() as conn, conn.transaction():
            return await self._set_password(conn, tenant_id, user_id, password_hash, at)

    async def put_account_token(self, token: AccountToken) -> None:
        """同一事务里删掉这名员工已有的链接，再写新的。"""
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    f"DELETE FROM {self._token} WHERE tenant_id = $1 AND user_id = $2",
                    token.tenant_id,
                    token.user_id,
                )
                await conn.execute(
                    f"INSERT INTO {self._token} ({_TOKEN_COLUMNS}) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    token.token_hash,
                    token.tenant_id,
                    token.user_id,
                    token.purpose,
                    token.created_at,
                    token.expires_at,
                    token.issued_by,
                )
        except asyncpg.ForeignKeyViolationError as exc:
            raise NotFoundError(f"用户 {token.user_id!r} 不存在") from exc

    async def get_account_token(self, token_hash: str, *, now: float) -> AccountToken | None:
        """按摘要取未过期的设置链接（不带租户，见协议说明）。"""
        row = await self._fetchrow(
            f"SELECT {_TOKEN_COLUMNS} FROM {self._token} WHERE token_hash = $1 AND expires_at > $2",
            token_hash,
            now,
        )
        return _token(row) if row is not None else None

    async def redeem_account_token(
        self, token: AccountToken, password_hash: str, *, at: float
    ) -> bool:
        """删链接（必须仍是同一条）并设口令，两步在一个事务里。"""
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                status = await conn.execute(
                    f"DELETE FROM {self._token} WHERE token_hash = $1 AND tenant_id = $2 "
                    "AND user_id = $3 AND purpose = $4 AND created_at = $5",
                    token.token_hash,
                    token.tenant_id,
                    token.user_id,
                    token.purpose,
                    token.created_at,
                )
                if affected(status) == 0:
                    return False
                changed = await self._set_password(
                    conn, token.tenant_id, token.user_id, password_hash, at
                )
                if not changed:
                    # 没有凭据可设：抛错让事务回滚，链接保留原样
                    raise _NoCredentialError
                return True
        except _NoCredentialError:
            return False

    async def purge_expired_account_tokens(self, now: float) -> int:
        """删掉已过期的设置链接（跨租户，见协议说明）。"""
        async with self._pool.acquire() as conn:
            return affected(
                await conn.execute(f"DELETE FROM {self._token} WHERE expires_at <= $1", now)
            )


class _NoCredentialError(Exception):
    """兑现链接时员工没有凭据：只用来回滚事务。"""


__all__ = ["PostgresCredentialStore"]
