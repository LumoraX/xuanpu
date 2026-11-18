"""玄圃自有账号的存储协议（ADR 0019）：用户名口令凭据与一次性设置链接。

由身份存储经 ``IdentityStore.credentials`` 提供，与身份存储同一后端（内存或 Postgres）。

租户隔离：除两处例外，每个方法都带 ``tenant_id``。例外与登录会话相同：

- ``get_account_token``：兑现链接时浏览器只出示令牌，按摘要查；调用方之后一律用返回记录里的
  租户；
- ``purge_expired_account_tokens``：周期清理，跨全部租户删过期记录。

存储不读时钟：是否过期、是否锁定都按调用方给的 ``now`` 判断；锁定的阈值与时长是服务层的规则，
由调用方传入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from xuanpu_server.identity.credential_models import AccountToken, LocalCredential


class CredentialStore(Protocol):
    """用户名口令凭据与一次性设置链接的存储。"""

    async def create_credential(self, credential: LocalCredential) -> None:
        """开通凭据（口令可以为空，表示待设）。

        Raises:
            NotFoundError: 本租户没有这名员工。
            ConflictError: 这名员工已经有凭据，或用户名在本租户已被占用。
        """
        ...

    async def get_credential(self, tenant_id: str, user_id: str) -> LocalCredential | None:
        """按员工取凭据。"""
        ...

    async def find_credential(self, tenant_id: str, username: str) -> LocalCredential | None:
        """按规整后的用户名取凭据。"""
        ...

    async def record_login_failure(
        self, tenant_id: str, user_id: str, *, now: float, lock_after: int, lock_seconds: float
    ) -> LocalCredential | None:
        """原子地记一次登录失败，返回记完之后的凭据；没有这条凭据返回 None。

        连续失败数到达 ``lock_after`` 时锁到 ``now + lock_seconds``，并把计数清零（解锁后再给
        ``lock_after`` 次机会）。
        """
        ...

    async def record_login_success(
        self, tenant_id: str, user_id: str, *, rehashed: str | None = None
    ) -> None:
        """登录成功：清零失败计数、解除锁定。``rehashed`` 给了就换成这个摘要（参数升级后的
        重算），不改 ``password_changed_at``。"""
        ...

    async def set_password(
        self, tenant_id: str, user_id: str, password_hash: str, *, at: float
    ) -> bool:
        """设口令：写摘要与设口令时间，清零失败计数、解除锁定，并删掉这名员工未兑现的设置链接。
        没有这条凭据返回 False。"""
        ...

    async def put_account_token(self, token: AccountToken) -> None:
        """签发设置链接；这名员工已有的链接被替换（旧链接立即失效）。

        Raises:
            NotFoundError: 本租户没有这名员工。
        """
        ...

    async def get_account_token(self, token_hash: str, *, now: float) -> AccountToken | None:
        """按令牌摘要取未过期的设置链接，不删（例外之一：不带租户）。"""
        ...

    async def redeem_account_token(
        self, token: AccountToken, password_hash: str, *, at: float
    ) -> bool:
        """原子地兑现设置链接：删掉这条链接并设口令（同 ``set_password``）。

        链接已经被兑现、被替换或这名员工没有凭据时什么都不改，返回 False——同一条链接并发兑现
        至多一个成功。
        """
        ...

    async def purge_expired_account_tokens(self, now: float) -> int:
        """删掉全部租户里已过期的设置链接，返回删掉的条数（例外之二：跨租户）。"""
        ...


__all__ = ["CredentialStore"]
