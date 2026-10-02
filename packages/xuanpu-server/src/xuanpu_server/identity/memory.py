"""身份存储的内存实现：用于测试与单机试用。进程退出即丢失。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.identity.models import (
    ReconcileState,
    UserUsageRecord,
    require_identity_of,
    require_page_size,
    require_status_change,
)

if TYPE_CHECKING:
    from xuanpu_server.identity.models import (
        DisabledReason,
        ExternalIdentity,
        IdentityProvider,
        LoginAttempt,
        LoginSession,
        TenantIdentityConfig,
        UserQuery,
        UserRecord,
        UserRole,
        UserStatus,
    )


class InMemoryIdentityStore:
    """内存里的身份存储。

    所有写操作在一把锁里完成；方法内部没有让出点，所以读操作不需要加锁，判断与写入也天然
    原子（取出即删、租约认领、条件更新）。
    """

    def __init__(self) -> None:
        """从空状态开始。"""
        self._lock = asyncio.Lock()
        self._users: dict[tuple[str, str], UserRecord] = {}
        self._identities: dict[tuple[str, str, str], ExternalIdentity] = {}
        self._configs: dict[tuple[str, str], TenantIdentityConfig] = {}
        self._logins: dict[str, LoginSession] = {}
        self._attempts: dict[str, LoginAttempt] = {}
        self._usage: dict[tuple[str, str, str], UserUsageRecord] = {}
        self._reconcile: dict[str, ReconcileState] = {}

    # --- 用户 ---

    def _require_user(self, tenant_id: str, user_id: str) -> UserRecord:
        """取用户，不存在则报错。"""
        user = self._users.get((tenant_id, user_id))
        if user is None:
            raise NotFoundError(f"用户 {user_id!r} 不存在")
        return user

    def _identity_key(self, identity: ExternalIdentity) -> tuple[str, str, str]:
        """外部身份的唯一键。"""
        return (identity.tenant_id, identity.provider, identity.subject)

    async def create_user(
        self, user: UserRecord, *, identity: ExternalIdentity | None = None
    ) -> None:
        """新建用户，可一并建外部身份。"""
        if identity is not None:
            require_identity_of(user, identity)
        async with self._lock:
            if (user.tenant_id, user.user_id) in self._users:
                raise ConflictError(f"用户 {user.user_id!r} 已存在")
            if identity is not None:
                if self._identity_key(identity) in self._identities:
                    raise ConflictError("外部身份已登记")
                self._identities[self._identity_key(identity)] = identity
            self._users[(user.tenant_id, user.user_id)] = user

    async def get_user(self, tenant_id: str, user_id: str) -> UserRecord | None:
        """按标识取用户。"""
        return self._users.get((tenant_id, user_id))

    async def get_user_by_identity(
        self, tenant_id: str, provider: IdentityProvider, subject: str
    ) -> UserRecord | None:
        """按外部身份取用户。"""
        identity = self._identities.get((tenant_id, provider, subject))
        if identity is None:
            return None
        return self._users.get((tenant_id, identity.user_id))

    async def list_users(self, tenant_id: str, query: UserQuery) -> list[UserRecord]:
        """按条件列出用户。"""
        found = [
            user
            for (owner, _), user in self._users.items()
            if owner == tenant_id and query.matches(user)
        ]
        found.sort(key=lambda user: user.user_id)
        return found[: query.limit]

    async def set_user_role(self, tenant_id: str, user_id: str, role: UserRole) -> UserRecord:
        """改角色。"""
        async with self._lock:
            updated = replace(self._require_user(tenant_id, user_id), role=role)
            self._users[(tenant_id, user_id)] = updated
            return updated

    async def set_user_status(
        self,
        tenant_id: str,
        user_id: str,
        status: UserStatus,
        *,
        reason: DisabledReason | None = None,
        at: float,
    ) -> bool:
        """改状态，返回是否真的变了。"""
        require_status_change(status, reason)
        async with self._lock:
            user = self._require_user(tenant_id, user_id)
            if user.status == status:
                return False
            disabled = status == "disabled"
            self._users[(tenant_id, user_id)] = replace(
                user,
                status=status,
                disabled_reason=reason,
                disabled_at=at if disabled else None,
            )
            return True

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
        async with self._lock:
            user = self._require_user(tenant_id, user_id)
            updated = replace(
                user,
                display_name=display_name,
                avatar_url=avatar_url,
                last_login_at=last_login_at if last_login_at is not None else user.last_login_at,
            )
            self._users[(tenant_id, user_id)] = updated
            return updated

    async def mark_user_unverified(self, tenant_id: str, user_id: str, *, at: float) -> bool:
        """标记查不到；已有起始时间的不动。"""
        async with self._lock:
            user = self._require_user(tenant_id, user_id)
            if user.unverified_since is not None:
                return False
            self._users[(tenant_id, user_id)] = replace(user, unverified_since=at)
            return True

    async def clear_user_unverified(self, tenant_id: str, user_id: str) -> bool:
        """清掉查不到标记。"""
        async with self._lock:
            user = self._require_user(tenant_id, user_id)
            if user.unverified_since is None:
                return False
            self._users[(tenant_id, user_id)] = replace(user, unverified_since=None)
            return True

    # --- 外部身份 ---

    async def create_external_identity(self, identity: ExternalIdentity) -> None:
        """给已有用户登记外部身份。"""
        async with self._lock:
            if self._identity_key(identity) in self._identities:
                raise ConflictError("外部身份已登记")
            self._require_user(identity.tenant_id, identity.user_id)
            self._identities[self._identity_key(identity)] = identity

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
        found = [
            identity
            for (owner, source, subject), identity in self._identities.items()
            if owner == tenant_id
            and source == provider
            and (user_id is None or identity.user_id == user_id)
            and (after_subject is None or subject > after_subject)
            and (user_status is None or self._user_status(identity) == user_status)
        ]
        found.sort(key=lambda identity: identity.subject)
        return found[:limit]

    def _user_status(self, identity: ExternalIdentity) -> str:
        """外部身份对应用户的状态。外部身份只能挂在已有用户上，用户不会被删，所以必有。"""
        return self._users[(identity.tenant_id, identity.user_id)].status

    async def update_identity_attributes(
        self, tenant_id: str, provider: IdentityProvider, subject: str, attributes: dict[str, str]
    ) -> ExternalIdentity:
        """整体替换外部身份的属性。"""
        key = (tenant_id, provider, subject)
        async with self._lock:
            identity = self._identities.get(key)
            if identity is None:
                raise NotFoundError("外部身份不存在")
            updated = replace(identity, attributes=dict(attributes))
            self._identities[key] = updated
            return updated

    # --- 租户身份配置 ---

    async def get_identity_config(
        self, tenant_id: str, provider: IdentityProvider
    ) -> TenantIdentityConfig | None:
        """取配置。"""
        return self._configs.get((tenant_id, provider))

    async def put_identity_config(self, config: TenantIdentityConfig) -> None:
        """写入或整条替换配置。"""
        async with self._lock:
            self._configs[(config.tenant_id, config.provider)] = config

    async def bind_tenant_key(
        self, tenant_id: str, provider: IdentityProvider, tenant_key: str
    ) -> str:
        """首次绑定企业标识。"""
        async with self._lock:
            config = self._configs.get((tenant_id, provider))
            if config is None:
                raise NotFoundError(f"租户 {tenant_id!r} 没有配置 {provider}")
            if config.feishu_tenant_key is not None:
                return config.feishu_tenant_key
            self._configs[(tenant_id, provider)] = replace(config, feishu_tenant_key=tenant_key)
            return tenant_key

    async def list_identity_configs(self, provider: IdentityProvider) -> list[TenantIdentityConfig]:
        """列出配置了某身份源的全部租户的配置。"""
        found = [config for config in self._configs.values() if config.provider == provider]
        return sorted(found, key=lambda config: config.tenant_id)

    # --- 登录会话 ---

    async def create_login_session(self, session: LoginSession) -> None:
        """登记登录会话。"""
        async with self._lock:
            if session.token_hash in self._logins:
                raise ConflictError("登录令牌冲突")
            self._require_user(session.tenant_id, session.user_id)
            self._logins[session.token_hash] = session

    async def get_login_session(self, token_hash: str, *, now: float) -> LoginSession | None:
        """按令牌摘要取未过期的登录会话。"""
        session = self._logins.get(token_hash)
        if session is None or session.expires_at <= now:
            return None
        return session

    def _owned_login(self, tenant_id: str, token_hash: str) -> LoginSession | None:
        """本租户的登录会话；别的租户的按不存在处理。"""
        session = self._logins.get(token_hash)
        return session if session is not None and session.tenant_id == tenant_id else None

    async def touch_login_session(self, tenant_id: str, token_hash: str, *, at: float) -> bool:
        """刷新最近使用时间。"""
        async with self._lock:
            session = self._owned_login(tenant_id, token_hash)
            if session is None:
                return False
            self._logins[token_hash] = replace(session, last_seen_at=at)
            return True

    async def delete_login_session(self, tenant_id: str, token_hash: str) -> bool:
        """删除一个登录会话。"""
        async with self._lock:
            if self._owned_login(tenant_id, token_hash) is None:
                return False
            del self._logins[token_hash]
            return True

    async def delete_user_login_sessions(self, tenant_id: str, user_id: str) -> int:
        """删除某用户的全部登录会话。"""
        async with self._lock:
            doomed = [
                token_hash
                for token_hash, session in self._logins.items()
                if session.tenant_id == tenant_id and session.user_id == user_id
            ]
            for token_hash in doomed:
                del self._logins[token_hash]
            return len(doomed)

    # --- 登录尝试 ---

    async def create_login_attempt(self, attempt: LoginAttempt) -> None:
        """登记登录尝试。"""
        async with self._lock:
            if attempt.state in self._attempts:
                raise ConflictError("登录尝试的 state 冲突")
            self._attempts[attempt.state] = attempt

    async def take_login_attempt(self, state: str, *, now: float) -> LoginAttempt | None:
        """取出并删除登录尝试；过期的删掉但不返回。"""
        async with self._lock:
            attempt = self._attempts.pop(state, None)
        if attempt is None or attempt.expires_at <= now:
            return None
        return attempt

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
        key = (tenant_id, day, user_id)
        async with self._lock:
            current = self._usage.get(key, UserUsageRecord(tenant_id, day, user_id))
            updated = UserUsageRecord(
                tenant_id=tenant_id,
                day=day,
                user_id=user_id,
                input_tokens=current.input_tokens + input_tokens,
                output_tokens=current.output_tokens + output_tokens,
                total_tokens=current.total_tokens + total_tokens,
                turns=current.turns + turns,
            )
            self._usage[key] = updated
            return updated

    async def list_user_usage(
        self, tenant_id: str, *, since: str, until: str, user_id: str | None = None
    ) -> list[UserUsageRecord]:
        """列出一段日期内的按用户用量。"""
        found = [
            record
            for (owner, day, who), record in self._usage.items()
            if owner == tenant_id and since <= day <= until and user_id in (None, who)
        ]
        return sorted(found, key=lambda record: (record.day, record.user_id))

    # --- 对账租约 ---

    async def claim_reconcile(
        self, tenant_id: str, *, owner: str, now: float, interval: float, lease_seconds: float
    ) -> bool:
        """认领对账。"""
        async with self._lock:
            state = self._reconcile.get(tenant_id, ReconcileState(tenant_id))
            if not state.claimable(now, interval):
                return False
            self._reconcile[tenant_id] = replace(
                state, lease_owner=owner, lease_expires_at=now + lease_seconds
            )
            return True

    async def finish_reconcile(self, tenant_id: str, *, owner: str, now: float) -> bool:
        """完成对账并放开租约。"""
        async with self._lock:
            state = self._reconcile.get(tenant_id)
            if state is None or state.lease_owner != owner:
                return False
            self._reconcile[tenant_id] = ReconcileState(tenant_id, last_run_at=now)
            return True

    async def get_reconcile_state(self, tenant_id: str) -> ReconcileState | None:
        """取对账状态。"""
        return self._reconcile.get(tenant_id)


__all__ = ["InMemoryIdentityStore"]
