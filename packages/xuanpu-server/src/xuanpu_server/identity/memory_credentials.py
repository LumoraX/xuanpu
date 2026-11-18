"""自有账号存储的内存实现：用于测试与单机试用，由 ``InMemoryIdentityStore.credentials`` 提供。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.identity.credential_models import AccountToken, LocalCredential


class InMemoryCredentialStore:
    """内存里的凭据与设置链接。

    写操作在一把锁里完成，锁内没有让出点，判断与写入天然原子。员工是否存在以身份存储为准
    （与 Postgres 的外键一致）；员工只会停用、不会删除，所以进锁前查一次不会误判。
    """

    def __init__(self, user_exists: Callable[[str, str], Awaitable[bool]]) -> None:
        """
        Args:
            user_exists: 判断 ``(tenant_id, user_id)`` 的员工是否存在。
        """
        self._user_exists = user_exists
        self._lock = asyncio.Lock()
        self._credentials: dict[tuple[str, str], LocalCredential] = {}
        self._tokens: dict[str, AccountToken] = {}

    async def _require_user(self, tenant_id: str, user_id: str) -> None:
        """员工不存在则报错。"""
        if not await self._user_exists(tenant_id, user_id):
            raise NotFoundError(f"用户 {user_id!r} 不存在")

    async def create_credential(self, credential: LocalCredential) -> None:
        """开通凭据。"""
        await self._require_user(credential.tenant_id, credential.user_id)
        async with self._lock:
            key = (credential.tenant_id, credential.user_id)
            if key in self._credentials:
                raise ConflictError("这名员工已经开通了用户名口令")
            if self._by_username(credential.tenant_id, credential.username) is not None:
                raise ConflictError(f"用户名 {credential.username!r} 已被占用")
            self._credentials[key] = credential

    def _by_username(self, tenant_id: str, username: str) -> LocalCredential | None:
        """按用户名找凭据（锁内外都可调用：只读）。"""
        for credential in self._credentials.values():
            if credential.tenant_id == tenant_id and credential.username == username:
                return credential
        return None

    async def get_credential(self, tenant_id: str, user_id: str) -> LocalCredential | None:
        """按员工取凭据。"""
        return self._credentials.get((tenant_id, user_id))

    async def find_credential(self, tenant_id: str, username: str) -> LocalCredential | None:
        """按用户名取凭据。"""
        return self._by_username(tenant_id, username)

    async def record_login_failure(
        self, tenant_id: str, user_id: str, *, now: float, lock_after: int, lock_seconds: float
    ) -> LocalCredential | None:
        """记一次失败；到阈值就锁定并清零计数。"""
        async with self._lock:
            current = self._credentials.get((tenant_id, user_id))
            if current is None:
                return None
            failures = current.failed_attempts + 1
            if failures >= lock_after:
                updated = replace(current, failed_attempts=0, locked_until=now + lock_seconds)
            else:
                updated = replace(current, failed_attempts=failures)
            self._credentials[(tenant_id, user_id)] = updated
            return updated

    async def record_login_success(
        self, tenant_id: str, user_id: str, *, rehashed: str | None = None
    ) -> None:
        """清零失败计数、解除锁定，按需换摘要。"""
        async with self._lock:
            current = self._credentials.get((tenant_id, user_id))
            if current is None:
                return
            password_hash = rehashed if rehashed is not None else current.password_hash
            self._credentials[(tenant_id, user_id)] = replace(
                current, failed_attempts=0, locked_until=None, password_hash=password_hash
            )

    def _set_password(self, tenant_id: str, user_id: str, password_hash: str, at: float) -> bool:
        """锁内调用：写口令、清计数、删掉这名员工的设置链接。"""
        current = self._credentials.get((tenant_id, user_id))
        if current is None:
            return False
        self._credentials[(tenant_id, user_id)] = replace(
            current,
            password_hash=password_hash,
            password_changed_at=at,
            failed_attempts=0,
            locked_until=None,
        )
        for token_hash, token in list(self._tokens.items()):
            if (token.tenant_id, token.user_id) == (tenant_id, user_id):
                del self._tokens[token_hash]
        return True

    async def set_password(
        self, tenant_id: str, user_id: str, password_hash: str, *, at: float
    ) -> bool:
        """设口令。"""
        async with self._lock:
            return self._set_password(tenant_id, user_id, password_hash, at)

    async def put_account_token(self, token: AccountToken) -> None:
        """签发设置链接，替换这名员工已有的。"""
        await self._require_user(token.tenant_id, token.user_id)
        async with self._lock:
            for token_hash, existing in list(self._tokens.items()):
                if (existing.tenant_id, existing.user_id) == (token.tenant_id, token.user_id):
                    del self._tokens[token_hash]
            self._tokens[token.token_hash] = token

    async def get_account_token(self, token_hash: str, *, now: float) -> AccountToken | None:
        """取未过期的设置链接。"""
        token = self._tokens.get(token_hash)
        return token if token is not None and token.expires_at > now else None

    async def redeem_account_token(
        self, token: AccountToken, password_hash: str, *, at: float
    ) -> bool:
        """删链接并设口令；链接已不在（被兑现或替换）就什么都不改。"""
        async with self._lock:
            if self._tokens.get(token.token_hash) != token:
                return False
            if (token.tenant_id, token.user_id) not in self._credentials:
                return False
            del self._tokens[token.token_hash]
            return self._set_password(token.tenant_id, token.user_id, password_hash, at)

    async def purge_expired_account_tokens(self, now: float) -> int:
        """删掉已过期的设置链接。"""
        async with self._lock:
            expired = [key for key, token in self._tokens.items() if token.expires_at <= now]
            for key in expired:
                del self._tokens[key]
            return len(expired)


__all__ = ["InMemoryCredentialStore"]
