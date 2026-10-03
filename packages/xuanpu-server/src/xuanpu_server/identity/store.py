"""身份存储协议。

身份域的持久状态都经这个协议读写：用户、外部身份、租户身份配置、登录会话、登录尝试、按用户
用量、对账状态。

**租户隔离**（红线 5）：除下面四个方法外，每个方法的第一个参数都是 ``tenant_id``，存储按它
过滤，别的租户的数据按不存在处理（取不到、改不了、删不掉）。四个例外都发生在知道租户之前，
或本来就跨租户，与 ``ControlPlaneStore.get_api_key``、``list_tenants`` 同理；拿到记录后，
调用方一律从记录里取租户：

- ``get_login_session``：请求只带登录 cookie，按令牌摘要才能找到会话与它的租户；
- ``take_login_attempt``：身份源的回调只带 ``state``，按它才能找回这次登录；
- ``list_identity_configs``：对账要遍历配置了某身份源的全部租户；
- ``purge_expired_logins``：维护任务清理全部租户过期的登录会话与登录尝试。

**租户必须存在**：建记录的方法（``create_user``、``put_identity_config``、
``create_login_attempt``、``add_user_usage``、``claim_reconcile``）在租户不存在时报
``NotFoundError``。租户以控制面为准：Postgres 实现靠外键，内存实现构造时拿到控制面存储去查。

**时间**：存储不读时钟。凡与时间有关的判断（登录会话、登录尝试是否过期，对账租约），都按调用方
给的 ``now`` 做；有效期多长是服务层的规则，服务层建记录时把它算进 ``expires_at``。

**不放业务规则**：比如「停用用户时删掉他的登录会话」「哪些操作要保住最后一名管理员」，都由
服务层决定与编排；这里只提供原子的读写，以及让这些编排在并发下仍然正确所需的原子条件更新：
状态是否真的变了、企业标识只绑定一次、取出即删、租约认领，以及改角色与状态时可选的「至少留一名
启用的管理员」（``keep_an_admin``）。

两个实现：``InMemoryIdentityStore`` 用于测试与单机试用，``PostgresIdentityStore`` 用于多实例
部署。二者对同一套契约测试负责。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from xuanpu_server.identity.models import (
        DisabledReason,
        ExternalIdentity,
        IdentityProvider,
        LoginAttempt,
        LoginSession,
        ReconcileState,
        TenantIdentityConfig,
        UserQuery,
        UserRecord,
        UserStatus,
        UserUsageRecord,
    )
    from xuanpu_server.tenancy import TenantRole


@runtime_checkable
class IdentityStore(Protocol):
    """身份存储。"""

    # --- 用户 ---

    async def create_user(
        self, user: UserRecord, *, identity: ExternalIdentity | None = None
    ) -> None:
        """新建用户；给了 ``identity`` 就在同一个原子操作里一并建它的外部身份。

        两者要么都建成、要么都不建：首次登录并发（同一人开了两个标签页）时，后到的那次因外部
        身份冲突失败，不会留下一个没有外部身份的孤儿用户；调用方捕获冲突后按外部身份重新取。

        Raises:
            ValueError: ``identity`` 的租户或用户与 ``user`` 不一致。
            NotFoundError: 租户不存在。
            ConflictError: 用户标识已存在，或外部身份 ``(tenant_id, provider, subject)`` 已存在。
        """
        ...

    async def get_user(self, tenant_id: str, user_id: str) -> UserRecord | None:
        """按标识取用户。"""
        ...

    async def get_user_by_identity(
        self, tenant_id: str, provider: IdentityProvider, subject: str
    ) -> UserRecord | None:
        """按外部身份取用户（登录时按飞书 ``union_id`` 找人）。"""
        ...

    async def list_users(self, tenant_id: str, query: UserQuery) -> list[UserRecord]:
        """按条件列出用户，按 ``user_id`` 升序；用 ``query.after`` 翻页。"""
        ...

    async def set_user_role(
        self, tenant_id: str, user_id: str, role: TenantRole, *, keep_an_admin: bool = False
    ) -> UserRecord:
        """改角色，返回改后的记录。

        ``keep_an_admin`` 为真时，若这次改动会让租户里不剩任何启用的 ``tenant_admin``（即把
        唯一一名启用的管理员降为成员），原子地拒绝：判断与写入之间别的改动插不进来，两名管理员
        同时互相降级，至多一个成功。

        Raises:
            NotFoundError: 用户不存在。
            ConflictError: ``keep_an_admin`` 为真且这是最后一名启用的管理员。
        """
        ...

    async def set_user_status(
        self,
        tenant_id: str,
        user_id: str,
        status: UserStatus,
        *,
        reason: DisabledReason | None = None,
        at: float,
        keep_an_admin: bool = False,
    ) -> bool:
        """改状态，返回状态是否真的变了。判断与写入是原子的，并发停用同一人只有一次返回 True。

        停用（``disabled``）必须给 ``reason``，``at`` 记为停用时间；已经是停用的不改，原因与
        时间保持第一次的。启用（``active``）不能给 ``reason``，并清掉停用原因与时间；``at``
        不落库。``keep_an_admin`` 与 ``set_user_role`` 相同：为真时不停用最后一名启用的管理员。

        Raises:
            ValueError: ``status`` 与 ``reason`` 不自洽。
            NotFoundError: 用户不存在。
            ConflictError: ``keep_an_admin`` 为真且这是最后一名启用的管理员。
        """
        ...

    async def update_user_profile(
        self,
        tenant_id: str,
        user_id: str,
        *,
        display_name: str,
        avatar_url: str | None,
        last_login_at: float | None = None,
    ) -> UserRecord:
        """刷新显示名与头像；``last_login_at`` 给了才改登录时间。返回改后的记录。

        Raises:
            NotFoundError: 用户不存在。
        """
        ...

    async def mark_user_unverified(self, tenant_id: str, user_id: str, *, at: float) -> bool:
        """标记对账时查不到此人：``unverified_since`` 为空才写入 ``at``，已有的起始时间不动。

        返回是否新标记了。

        Raises:
            NotFoundError: 用户不存在。
        """
        ...

    async def clear_user_unverified(self, tenant_id: str, user_id: str) -> bool:
        """清掉「查不到」标记，返回是否确实清掉了。

        Raises:
            NotFoundError: 用户不存在。
        """
        ...

    # --- 外部身份 ---

    async def create_external_identity(self, identity: ExternalIdentity) -> None:
        """给已有用户登记一个外部身份。一名用户在一个身份源里只有一个外部身份。

        Raises:
            ConflictError: ``(tenant_id, provider, subject)`` 已存在，或这名用户在该身份源里
                已有外部身份。
            NotFoundError: 本租户里没有这名用户。
        """
        ...

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
        """列出某身份源下的外部身份，按 ``subject`` 升序；用 ``after_subject`` 翻页。

        ``user_id`` 只看某个用户的；``user_status`` 按对应用户的状态过滤（对账取启用的人）。

        Raises:
            ValueError: ``limit`` 超出 1 到 1000。
        """
        ...

    async def update_identity_attributes(
        self, tenant_id: str, provider: IdentityProvider, subject: str, attributes: dict[str, str]
    ) -> ExternalIdentity:
        """整体替换外部身份的属性（登录时刷新 ``open_id``），返回改后的记录。

        Raises:
            NotFoundError: 外部身份不存在。
        """
        ...

    # --- 租户身份配置 ---

    async def get_identity_config(
        self, tenant_id: str, provider: IdentityProvider
    ) -> TenantIdentityConfig | None:
        """取租户接入某身份源的配置。"""
        ...

    async def put_identity_config(self, config: TenantIdentityConfig) -> None:
        """写入配置，或替换已有配置里除企业标识之外的各项。

        已绑定的 ``feishu_tenant_key`` 原样保留：绑定只能经 ``bind_tenant_key``，否则重写一次
        配置就能把绑定清掉、再绑到别的企业上。

        Raises:
            ValueError: ``config.feishu_tenant_key`` 不为空。
            NotFoundError: 租户不存在。
        """
        ...

    async def bind_tenant_key(
        self, tenant_id: str, provider: IdentityProvider, tenant_key: str
    ) -> str:
        """首次绑定身份源的企业标识：还没绑定才写入，已绑定的不覆盖。原子操作。

        返回绑定后的值（可能是之前绑定的），调用方与本次登录的企业标识比对。

        Raises:
            NotFoundError: 租户没有这个身份源的配置。
        """
        ...

    async def list_identity_configs(self, provider: IdentityProvider) -> list[TenantIdentityConfig]:
        """列出配置了某身份源的全部租户的配置，按租户标识排序。不带租户：对账用。"""
        ...

    # --- 登录会话 ---

    async def create_login_session(self, session: LoginSession) -> None:
        """登记一个登录会话。

        Raises:
            ConflictError: 令牌摘要已存在。
            NotFoundError: 本租户里没有这名用户。
        """
        ...

    async def get_login_session(self, token_hash: str, *, now: float) -> LoginSession | None:
        """按令牌摘要取登录会话；不存在或 ``expires_at <= now`` 时返回 None。不带租户：见模块说明。

        只读：过期的不在这里删，由 ``purge_expired_logins`` 清理。
        """
        ...

    async def touch_login_session(self, tenant_id: str, token_hash: str, *, at: float) -> bool:
        """把 ``last_seen_at`` 写成 ``at``，返回是否确实写了。多久写一次由服务层节流。"""
        ...

    async def delete_login_session(self, tenant_id: str, token_hash: str) -> bool:
        """删除一个登录会话（登出），返回是否确实删掉了。"""
        ...

    async def delete_user_login_sessions(self, tenant_id: str, user_id: str) -> int:
        """删除某用户的全部登录会话，返回删掉的个数。"""
        ...

    # --- 登录尝试 ---

    async def create_login_attempt(self, attempt: LoginAttempt) -> None:
        """登记一次登录尝试。

        Raises:
            ConflictError: ``state`` 已存在。
            NotFoundError: 租户不存在。
        """
        ...

    async def take_login_attempt(self, state: str, *, now: float) -> LoginAttempt | None:
        """取出并删除登录尝试，只能取一次。不带租户：见模块说明。

        取出与删除是原子的：并发取同一个 ``state``，只有一个拿到。``expires_at <= now`` 的
        同样删除，但返回 None。
        """
        ...

    async def purge_expired_logins(self, now: float) -> int:
        """删除全部租户里 ``expires_at <= now`` 的登录会话与登录尝试，返回删掉的总条数。

        不带租户的维护操作（见模块说明），由周期任务调用：没走完的登录、过期没登出的会话都在
        这里清掉。
        """
        ...

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
        """把一笔用量累加到某人某一天，返回累加后的结果。累加是原子的。

        Raises:
            NotFoundError: 租户不存在。
        """
        ...

    async def list_user_usage(
        self, tenant_id: str, *, since: str, until: str, user_id: str | None = None
    ) -> list[UserUsageRecord]:
        """列出一段日期（两端都含）内有记录的用量，按日期、用户排序；``user_id`` 只看某人的。"""
        ...

    # --- 对账租约 ---

    async def claim_reconcile(
        self, tenant_id: str, *, owner: str, now: float, interval: float, lease_seconds: float
    ) -> bool:
        """认领租户的对账，返回是否认领成功。判断与写入是原子的。

        条件见 ``ReconcileState.claimable``：没有未到期的持有者（持有者自己也不行），且距上次
        完成已满 ``interval`` 秒（手工触发传 0）。成功后持有到 ``now + lease_seconds``。

        Raises:
            ValueError: ``interval`` 为负，或 ``lease_seconds`` 不为正。
            NotFoundError: 租户不存在。
        """
        ...

    async def renew_reconcile(
        self, tenant_id: str, *, owner: str, now: float, lease_seconds: float
    ) -> bool:
        """仅当前持有者且租约未过期时原子续期；过期不能复活。"""
        ...

    async def release_reconcile(self, tenant_id: str, *, owner: str) -> bool:
        """失败或取消时按持有者释放租约，保留上次成功完成时间。"""
        ...

    async def finish_reconcile(self, tenant_id: str, *, owner: str, now: float) -> bool:
        """完成对账：记下完成时间 ``now`` 并放开租约。只有当前持有者能完成，返回是否完成了。

        租约已到期但还没被别人接手的，原持有者仍可完成；已被别人接手的不行。
        """
        ...

    async def get_reconcile_state(self, tenant_id: str) -> ReconcileState | None:
        """取租户的对账状态；从未认领过返回 None。"""
        ...


__all__ = ["IdentityStore"]
