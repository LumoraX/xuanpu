"""身份存储的 Postgres 实现：用于多实例部署。

与控制面共用连接池与命名空间（同一个 schema、同一个表名前缀），表由控制面的迁移
``0004_identity`` 建出。租户隔离：除 ``IdentityStore`` 模块说明里的三个例外，每条语句都带
``tenant_id`` 条件。表名来自命名空间（字符集已校验），取值一律走参数绑定。

按标识排序与翻页的比较用 ``COLLATE "C"``，即按码点比较：与内存实现的字符串比较一致，也不受
库的默认排序规则影响。

用户与外部身份两张表的读写、连接池与语句助手在 ``identity.postgres_users``，这里继承它，补上
租户身份配置、登录会话、登录尝试、按用户用量与对账租约。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg

from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.identity.models import (
    LoginAttempt,
    LoginSession,
    ReconcileState,
    TenantIdentityConfig,
    UserUsageRecord,
)
from xuanpu_server.identity.postgres_users import PostgresUserTables

if TYPE_CHECKING:
    from xuanpu_server.identity.models import IdentityProvider

_CONFIG_COLUMNS = (
    "tenant_id, provider, app_id, app_secret_ref, encrypt_key_ref, verification_token_ref, "
    "feishu_tenant_key, session_ttl_seconds"
)


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
        """写入或整条替换配置。"""
        await self._execute(
            f"INSERT INTO {self._t['identity_config']} ({_CONFIG_COLUMNS}) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (tenant_id, provider) DO UPDATE "
            "SET app_id = EXCLUDED.app_id, app_secret_ref = EXCLUDED.app_secret_ref, "
            "encrypt_key_ref = EXCLUDED.encrypt_key_ref, "
            "verification_token_ref = EXCLUDED.verification_token_ref, "
            "feishu_tenant_key = EXCLUDED.feishu_tenant_key, "
            "session_ttl_seconds = EXCLUDED.session_ttl_seconds",
            config.tenant_id,
            config.provider,
            config.app_id,
            config.app_secret_ref,
            config.encrypt_key_ref,
            config.verification_token_ref,
            config.feishu_tenant_key,
            config.session_ttl_seconds,
        )

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
        """列出配置了某身份源的全部租户的配置。"""
        rows = await self._fetch(
            f"SELECT {_CONFIG_COLUMNS} FROM {self._t['identity_config']} WHERE provider = $1 "
            'ORDER BY tenant_id COLLATE "C"',
            provider,
        )
        return [TenantIdentityConfig(**dict(row)) for row in rows]

    # --- 登录会话 ---

    async def create_login_session(self, session: LoginSession) -> None:
        """登记登录会话。"""
        try:
            await self._execute(
                f"INSERT INTO {self._t['login_session']} (token_hash, tenant_id, user_id, "
                "created_at, expires_at, last_seen_at) VALUES ($1, $2, $3, $4, $5, $6)",
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
            f"SELECT * FROM {self._t['login_session']} WHERE token_hash = $1 AND expires_at > $2",
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
                f"INSERT INTO {self._t['login_attempt']} (state, tenant_id, code_verifier, "
                "return_to, created_at, expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
                attempt.state,
                attempt.tenant_id,
                attempt.code_verifier,
                attempt.return_to,
                attempt.created_at,
                attempt.expires_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("登录尝试的 state 冲突") from exc

    async def take_login_attempt(self, state: str, *, now: float) -> LoginAttempt | None:
        """取出并删除登录尝试。一条 ``DELETE … RETURNING``：并发删同一行只有一个拿到它。"""
        row = await self._fetchrow(
            f"DELETE FROM {self._t['login_attempt']} WHERE state = $1 RETURNING *", state
        )
        if row is None:
            return None
        attempt = LoginAttempt(**dict(row))
        return attempt if attempt.expires_at > now else None

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
        row = await self._fetchrow(
            f"INSERT INTO {self._t['user_usage']} AS u (tenant_id, day, user_id, input_tokens, "
            "output_tokens, total_tokens, turns) VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (tenant_id, day, user_id) DO UPDATE SET "
            "input_tokens = u.input_tokens + EXCLUDED.input_tokens, "
            "output_tokens = u.output_tokens + EXCLUDED.output_tokens, "
            "total_tokens = u.total_tokens + EXCLUDED.total_tokens, "
            "turns = u.turns + EXCLUDED.turns RETURNING *",
            tenant_id,
            day,
            user_id,
            input_tokens,
            output_tokens,
            total_tokens,
            turns,
        )
        assert row is not None
        return UserUsageRecord(**dict(row))

    async def list_user_usage(
        self, tenant_id: str, *, since: str, until: str, user_id: str | None = None
    ) -> list[UserUsageRecord]:
        """列出一段日期内的按用户用量。"""
        rows = await self._fetch(
            f"SELECT * FROM {self._t['user_usage']} "
            "WHERE tenant_id = $1 AND day >= $2 AND day <= $3 "
            "AND ($4::text IS NULL OR user_id = $4) "
            'ORDER BY day, user_id COLLATE "C"',
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
        table = self._t["identity_reconcile"]
        row = await self._fetchrow(
            f"INSERT INTO {table} AS r (tenant_id, last_run_at, lease_owner, lease_expires_at) "
            "VALUES ($1, NULL, $2, $5) ON CONFLICT (tenant_id) DO UPDATE "
            "SET lease_owner = EXCLUDED.lease_owner, lease_expires_at = EXCLUDED.lease_expires_at "
            "WHERE (r.lease_owner IS NULL OR r.lease_expires_at <= $3) "
            "AND (r.last_run_at IS NULL OR r.last_run_at + $4 <= $3) "
            "RETURNING r.tenant_id",
            tenant_id,
            owner,
            now,
            interval,
            now + lease_seconds,
        )
        return row is not None

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
            f"SELECT * FROM {self._t['identity_reconcile']} WHERE tenant_id = $1", tenant_id
        )
        return ReconcileState(**dict(row)) if row is not None else None


__all__ = ["PostgresIdentityStore"]
