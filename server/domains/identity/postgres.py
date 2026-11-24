"""身份存储的 Postgres 实现：用于多实例部署。

与控制面共用连接池与命名空间（同一个 schema、同一个表名前缀），表由控制面的迁移
``0004_identity`` 建出。租户隔离：除 ``IdentityStore`` 模块说明里的四个例外，每条语句都带
``tenant_id`` 条件。表名来自命名空间（字符集已校验），取值一律走参数绑定。

**读取一律写明列名**，不用 ``SELECT *`` / ``RETURNING *``：部署先迁移、再滚动升级实例，以后给
这些表加列时，还在跑的旧实例读到多出来的列也不会出错。

参与排序与翻页的文本键（用户标识、外部标识）在建表时声明为 ``COLLATE "C"``，按码点比较，与
内存实现一致，查询里不再写 ``COLLATE``，主键索引直接可用。租户不存在时，外键冲突转成
``NotFoundError``，与内存实现一致。

用户与外部身份两张表的读写、连接池与语句助手在 ``identity.postgres_users``，这里继承它，补上
租户身份配置、登录会话、登录尝试、按用户用量与对账租约。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from server.core.errors import ConflictError, NotFoundError
from server.domains.identity.models import (
    DesktopHandoff,
    LoginAttempt,
    LoginSession,
    ReconcileState,
    TenantIdentityConfig,
    UserUsageRecord,
    require_reconcile_window,
)
from server.domains.identity.postgres_users import PostgresUserTables, affected

if TYPE_CHECKING:
    from server.domains.identity.models import ExternalIdentity, IdentityProvider

_CONFIG_COLUMNS = (
    "tenant_id, provider, app_id, app_secret_ref, encrypt_key_ref, verification_token_ref, "
    "feishu_tenant_key, session_ttl_seconds"
)
_LOGIN_SESSION_COLUMNS = "token_hash, tenant_id, user_id, created_at, expires_at, last_seen_at"
_LOGIN_ATTEMPT_COLUMNS = (
    "state, tenant_id, code_verifier, return_to, created_at, expires_at, cookie_hash, client"
)
_HANDOFF_COLUMNS = (
    "cookie_hash, tenant_id, user_id, return_to, nonce_hash, confirmed, created_at, expires_at"
)
_USER_USAGE_COLUMNS = "tenant_id, day, user_id, input_tokens, output_tokens, total_tokens, turns"
_RECONCILE_COLUMNS = "tenant_id, last_run_at, lease_owner, lease_expires_at"


def _missing_tenant(tenant_id: str) -> NotFoundError:
    """租户不存在（外键冲突）时报的错。"""
    return NotFoundError(f"租户 {tenant_id!r} 不存在")


class PostgresIdentityStore(PostgresUserTables):
    """Postgres 里的身份存储。构造参数见 ``PostgresUserTables``。"""

    # --- 租户身份配置 ---

    async def get_identity_config(
        self, tenant_id: str, provider: IdentityProvider
    ) -> TenantIdentityConfig | None:
        """取配置。"""
        row = await self._fetchrow(
            f"SELECT {_CONFIG_COLUMNS} FROM {self._t['identity_config']} "
            "WHERE tenant_id = $1 AND provider = $2",
            tenant_id,
            provider,
        )
        return TenantIdentityConfig(**dict(row)) if row is not None else None

    async def put_identity_config(self, config: TenantIdentityConfig) -> None:
        """写入或替换配置。更新分支不碰 ``feishu_tenant_key``：已绑定的企业标识原样保留。"""
        if config.feishu_tenant_key is not None:
            raise ValueError("feishu_tenant_key 只能经 bind_tenant_key 绑定，写配置时须为空")
        try:
            await self._execute(
                f"INSERT INTO {self._t['identity_config']} (tenant_id, provider, app_id, "
                "app_secret_ref, encrypt_key_ref, verification_token_ref, session_ttl_seconds) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (tenant_id, provider) DO UPDATE "
                "SET app_id = EXCLUDED.app_id, app_secret_ref = EXCLUDED.app_secret_ref, "
                "encrypt_key_ref = EXCLUDED.encrypt_key_ref, "
                "verification_token_ref = EXCLUDED.verification_token_ref, "
                "session_ttl_seconds = EXCLUDED.session_ttl_seconds",
                config.tenant_id,
                config.provider,
                config.app_id,
                config.app_secret_ref,
                config.encrypt_key_ref,
                config.verification_token_ref,
                config.session_ttl_seconds,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _missing_tenant(config.tenant_id) from exc

    async def bind_tenant_key(
        self, tenant_id: str, provider: IdentityProvider, tenant_key: str
    ) -> str:
        """首次绑定企业标识；行锁让并发的绑定排队，后到的读到先到的值。"""
        row = await self._fetchrow(
            f"UPDATE {self._t['identity_config']} "
            "SET feishu_tenant_key = COALESCE(feishu_tenant_key, $3) "
            "WHERE tenant_id = $1 AND provider = $2 RETURNING feishu_tenant_key",
            tenant_id,
            provider,
            tenant_key,
        )
        if row is None:
            raise NotFoundError(f"租户 {tenant_id!r} 没有配置 {provider}")
        return str(row["feishu_tenant_key"])

    async def list_identity_configs(self, provider: IdentityProvider) -> list[TenantIdentityConfig]:
        """列出配置了某身份源的全部租户的配置。

        租户标识列与租户表一样用库的默认排序规则，这里在查询里按码点排序，与内存实现一致；只列
        配置过的租户，行数很少，不需要索引。
        """
        rows = await self._fetch(
            f"SELECT {_CONFIG_COLUMNS} FROM {self._t['identity_config']} WHERE provider = $1 "
            'ORDER BY tenant_id COLLATE "C"',
            provider,
        )
        return [TenantIdentityConfig(**dict(row)) for row in rows]

    # --- 登录会话 ---

    async def complete_external_login(self, identity: ExternalIdentity, session: LoginSession, *,
                                      bind: bool, required_login_hash: str | None = None,
                                      connection_id: str | None = None, connection_version: int | None = None) -> None:
        """同一连接锁住租户、员工及原登录，外部映射与新登录原子提交。"""
        from server.core.errors import AuthenticationError

        if (identity.tenant_id,identity.user_id)!=(session.tenant_id,session.user_id):
            raise ValueError('身份与登录会话不一致')
        t=self._t
        try:
            async with self._pool.acquire() as conn,conn.transaction():
                tenant=await conn.fetchval(f"SELECT status FROM {self._tenant_table} WHERE tenant_id=$1 FOR UPDATE",identity.tenant_id)
                user=await conn.fetchval(f"SELECT status FROM {t['user']} WHERE tenant_id=$1 AND user_id=$2 FOR UPDATE",identity.tenant_id,identity.user_id)
                if tenant!='active' or user!='active':
                    raise AuthenticationError('企业登录身份不可用')
                if connection_id is not None:
                    revision=await conn.fetchval(f"SELECT record FROM {self._connection_table} WHERE tenant_id=$1 AND id=$2",identity.tenant_id,connection_id)
                    import json
                    install=json.loads(revision) if revision is not None else {}
                    if install.get('status')!='active' or install.get('version')!=connection_version:
                        raise ConflictError('应用配置已变化，请重新登录')
                if bind:
                    old=await conn.fetchrow(f"SELECT user_id,expires_at FROM {t['login_session']} WHERE tenant_id=$1 AND token_hash=$2 FOR UPDATE",identity.tenant_id,required_login_hash)
                    if old is None or old['user_id']!=identity.user_id or old['expires_at']<=session.created_at:
                        raise AuthenticationError('绑定期间原登录已失效')
                existing=await conn.fetchval(f"SELECT user_id FROM {t['external_identity']} WHERE tenant_id=$1 AND provider=$2 AND subject=$3",identity.tenant_id,identity.provider,identity.subject)
                if existing is not None and existing!=identity.user_id:
                    raise ConflictError('企业身份已绑定其他员工')
                if existing is None and not bind:
                    raise AuthenticationError('企业身份尚未预配')
                if existing is None:
                    await self._insert_identity(conn,identity)
                await conn.execute(f"INSERT INTO {t['login_session']} ({_LOGIN_SESSION_COLUMNS}) VALUES($1,$2,$3,$4,$5,$6)",session.token_hash,session.tenant_id,session.user_id,session.created_at,session.expires_at,session.last_seen_at)
                if required_login_hash is not None:
                    await conn.execute(f"DELETE FROM {t['login_session']} WHERE tenant_id=$1 AND token_hash=$2",identity.tenant_id,required_login_hash)
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError('企业身份或登录会话已变化') from exc

    async def create_login_session(self, session: LoginSession) -> None:
        """登记登录会话。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['login_session']} ({_LOGIN_SESSION_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                session.token_hash,
                session.tenant_id,
                session.user_id,
                session.created_at,
                session.expires_at,
                session.last_seen_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("登录令牌冲突") from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise NotFoundError(f"用户 {session.user_id!r} 不存在") from exc

    async def get_login_session(self, token_hash: str, *, now: float) -> LoginSession | None:
        """按令牌摘要取未过期的登录会话。"""
        row = await self._fetchrow(
            f"SELECT {_LOGIN_SESSION_COLUMNS} FROM {self._t['login_session']} "
            "WHERE token_hash = $1 AND expires_at > $2",
            token_hash,
            now,
        )
        return LoginSession(**dict(row)) if row is not None else None

    async def touch_login_session(self, tenant_id: str, token_hash: str, *, at: float) -> bool:
        """刷新最近使用时间。"""
        changed = await self._execute(
            f"UPDATE {self._t['login_session']} SET last_seen_at = $3 "
            "WHERE tenant_id = $1 AND token_hash = $2",
            tenant_id,
            token_hash,
            at,
        )
        return bool(changed)

    async def delete_login_session(self, tenant_id: str, token_hash: str) -> bool:
        """删除一个登录会话。"""
        changed = await self._execute(
            f"DELETE FROM {self._t['login_session']} WHERE tenant_id = $1 AND token_hash = $2",
            tenant_id,
            token_hash,
        )
        return bool(changed)

    async def delete_user_login_sessions(self, tenant_id: str, user_id: str) -> int:
        """删除某用户的全部登录会话。"""
        return await self._execute(
            f"DELETE FROM {self._t['login_session']} WHERE tenant_id = $1 AND user_id = $2",
            tenant_id,
            user_id,
        )

    # --- 登录尝试 ---

    async def create_login_attempt(self, attempt: LoginAttempt) -> None:
        """登记登录尝试。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['login_attempt']} ({_LOGIN_ATTEMPT_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                attempt.state,
                attempt.tenant_id,
                attempt.code_verifier,
                attempt.return_to,
                attempt.created_at,
                attempt.expires_at,
                attempt.cookie_hash,
                attempt.client,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("登录尝试的 state 冲突") from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise _missing_tenant(attempt.tenant_id) from exc

    async def take_login_attempt(self, state: str, *, now: float) -> LoginAttempt | None:
        """取出并删除登录尝试。一条 ``DELETE … RETURNING``：并发删同一行只有一个拿到它。"""
        row = await self._fetchrow(
            f"DELETE FROM {self._t['login_attempt']} WHERE state = $1 "
            f"RETURNING {_LOGIN_ATTEMPT_COLUMNS}",
            state,
        )
        if row is None:
            return None
        attempt = LoginAttempt(**dict(row))
        return attempt if attempt.expires_at > now else None

    # --- 桌面接力 ---

    async def create_desktop_handoff(self, handoff: DesktopHandoff) -> None:
        """登记桌面接力。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['desktop_handoff']} ({_HANDOFF_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                handoff.cookie_hash,
                handoff.tenant_id,
                handoff.user_id,
                handoff.return_to,
                handoff.nonce_hash,
                handoff.confirmed,
                handoff.created_at,
                handoff.expires_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("桌面接力记录冲突") from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise _missing_tenant(handoff.tenant_id) from exc

    async def get_desktop_handoff(self, cookie_hash: str, *, now: float) -> DesktopHandoff | None:
        """按 cookie 摘要查接力记录；过期的不返回。"""
        row = await self._fetchrow(
            f"SELECT {_HANDOFF_COLUMNS} FROM {self._t['desktop_handoff']} "
            "WHERE cookie_hash = $1 AND expires_at > $2",
            cookie_hash,
            now,
        )
        return DesktopHandoff(**dict(row)) if row is not None else None

    async def confirm_desktop_handoff(
        self, nonce_hash: str, *, now: float
    ) -> DesktopHandoff | None:
        """一条条件 ``UPDATE``：只把未确认、未过期的标为已确认；并发确认只有一个成功。"""
        row = await self._fetchrow(
            f"UPDATE {self._t['desktop_handoff']} SET confirmed = TRUE "
            "WHERE nonce_hash = $1 AND NOT confirmed AND expires_at > $2 "
            f"RETURNING {_HANDOFF_COLUMNS}",
            nonce_hash,
            now,
        )
        return DesktopHandoff(**dict(row)) if row is not None else None

    async def take_desktop_handoff(self, cookie_hash: str, *, now: float) -> DesktopHandoff | None:
        """一条 ``DELETE … RETURNING``：只删已确认的；过期的同样删掉但不返回。"""
        row = await self._fetchrow(
            f"DELETE FROM {self._t['desktop_handoff']} WHERE cookie_hash = $1 AND confirmed "
            f"RETURNING {_HANDOFF_COLUMNS}",
            cookie_hash,
        )
        if row is None:
            return None
        handoff = DesktopHandoff(**dict(row))
        return handoff if handoff.expires_at > now else None

    async def purge_expired_logins(self, now: float) -> int:
        """删除全部租户里过期的登录会话、登录尝试与桌面接力（三张表都按 ``expires_at`` 建了索引）。"""
        purged = 0
        async with self._pool.acquire() as conn:
            for table in ("login_session", "login_attempt", "desktop_handoff"):
                status = await conn.execute(
                    f"DELETE FROM {self._t[table]} WHERE expires_at <= $1", now
                )
                purged += affected(status)
        return purged

    # --- 按用户用量 ---

    async def add_user_usage(
        self,
        tenant_id: str,
        day: str,
        user_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        turns: int,
    ) -> UserUsageRecord:
        """累加某人某天的用量。"""
        try:
            row = await self._fetchrow(
                f"INSERT INTO {self._t['user_usage']} AS u ({_USER_USAGE_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (tenant_id, day, user_id) DO UPDATE SET "
                "input_tokens = u.input_tokens + EXCLUDED.input_tokens, "
                "output_tokens = u.output_tokens + EXCLUDED.output_tokens, "
                "total_tokens = u.total_tokens + EXCLUDED.total_tokens, "
                f"turns = u.turns + EXCLUDED.turns RETURNING {_USER_USAGE_COLUMNS}",
                tenant_id,
                day,
                user_id,
                input_tokens,
                output_tokens,
                total_tokens,
                turns,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _missing_tenant(tenant_id) from exc
        assert row is not None
        return UserUsageRecord(**dict(row))

    async def list_user_usage(
        self, tenant_id: str, *, since: str, until: str, user_id: str | None = None
    ) -> list[UserUsageRecord]:
        """列出一段日期内的按用户用量（``user_id`` 列是 ``COLLATE "C"``）。"""
        rows = await self._fetch(
            f"SELECT {_USER_USAGE_COLUMNS} FROM {self._t['user_usage']} "
            "WHERE tenant_id = $1 AND day >= $2 AND day <= $3 "
            "AND ($4::text IS NULL OR user_id = $4) "
            "ORDER BY day, user_id",
            tenant_id,
            since,
            until,
            user_id,
        )
        return [UserUsageRecord(**dict(row)) for row in rows]

    # --- 对账租约 ---

    async def claim_reconcile(
        self, tenant_id: str, *, owner: str, now: float, interval: float, lease_seconds: float
    ) -> bool:
        """认领对账。条件与 ``ReconcileState.claimable`` 一致，在一条语句里判断并写入。

        首次认领走插入；并发的首次插入里后到的会等先到的提交，再按冲突分支判断条件，此时
        租约已被占，判断不通过。
        """
        require_reconcile_window(interval, lease_seconds)
        table = self._t["identity_reconcile"]
        try:
            row = await self._fetchrow(
                f"INSERT INTO {table} AS r ({_RECONCILE_COLUMNS}) "
                "VALUES ($1, NULL, $2, $5) ON CONFLICT (tenant_id) DO UPDATE "
                "SET lease_owner = EXCLUDED.lease_owner, "
                "lease_expires_at = EXCLUDED.lease_expires_at "
                "WHERE (r.lease_owner IS NULL OR r.lease_expires_at <= $3) "
                "AND (r.last_run_at IS NULL OR r.last_run_at + $4 <= $3) "
                "RETURNING r.tenant_id",
                tenant_id,
                owner,
                now,
                interval,
                now + lease_seconds,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise _missing_tenant(tenant_id) from exc
        return row is not None

    async def renew_reconcile(
        self, tenant_id: str, *, owner: str, now: float, lease_seconds: float
    ) -> bool:
        """原子续期仍有效且属于本持有者的租约。"""
        require_reconcile_window(0, lease_seconds)
        changed = await self._execute(
            f"UPDATE {self._t['identity_reconcile']} SET lease_expires_at = $4 "
            "WHERE tenant_id = $1 AND lease_owner = $2 AND lease_expires_at > $3",
            tenant_id,
            owner,
            now,
            now + lease_seconds,
        )
        return bool(changed)

    async def release_reconcile(self, tenant_id: str, *, owner: str) -> bool:
        """失败释放，不更新完成时间。"""
        changed = await self._execute(
            f"UPDATE {self._t['identity_reconcile']} "
            "SET lease_owner = NULL, lease_expires_at = 0 "
            "WHERE tenant_id = $1 AND lease_owner = $2",
            tenant_id,
            owner,
        )
        return bool(changed)

    async def finish_reconcile(self, tenant_id: str, *, owner: str, now: float) -> bool:
        """完成对账并放开租约。"""
        changed = await self._execute(
            f"UPDATE {self._t['identity_reconcile']} "
            "SET last_run_at = $3, lease_owner = NULL, lease_expires_at = 0 "
            "WHERE tenant_id = $1 AND lease_owner = $2",
            tenant_id,
            owner,
            now,
        )
        return bool(changed)

    async def get_reconcile_state(self, tenant_id: str) -> ReconcileState | None:
        """取对账状态。"""
        row = await self._fetchrow(
            f"SELECT {_RECONCILE_COLUMNS} FROM {self._t['identity_reconcile']} "
            "WHERE tenant_id = $1",
            tenant_id,
        )
        return ReconcileState(**dict(row)) if row is not None else None


__all__ = ["PostgresIdentityStore"]
