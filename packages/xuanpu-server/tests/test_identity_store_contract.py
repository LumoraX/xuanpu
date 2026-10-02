"""身份存储的契约测试：内存实现与 Postgres 实现各跑一遍。

过期判断的分工：有效期长度是服务层的规则，服务层建记录时把它算进 ``expires_at``；存储
不读时钟，只按调用方给的 ``now`` 判断「是否已过期」。所以这里的过期用例都显式传 ``now``。
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import pytest
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.identity import (
    ExternalIdentity,
    IdentityStore,
    LoginAttempt,
    LoginSession,
    ReconcileState,
    TenantIdentityConfig,
    UserQuery,
    UserRecord,
    new_user_id,
)
from xuanpu_server.tenancy import Tenant

if TYPE_CHECKING:
    from xuanpu_server.controlplane import ControlPlaneStore


@pytest.fixture
async def identity(identity_stores: tuple[ControlPlaneStore, IdentityStore]) -> IdentityStore:
    """身份存储，已开通租户 acme 与 other。"""
    control, store = identity_stores
    for tenant_id in ("acme", "other"):
        await control.create_tenant(Tenant(tenant_id=tenant_id, name=tenant_id, created_at=1.0))
    return store


def _user(tenant: str, user_id: str, **kwargs: object) -> UserRecord:
    """构造用户记录：默认普通成员、启用。"""
    fields: dict[str, object] = {"display_name": f"员工 {user_id}", "created_at": 10.0}
    fields.update(kwargs)
    return UserRecord(tenant_id=tenant, user_id=user_id, **fields)  # type: ignore[arg-type]


def _identity(tenant: str, subject: str, user_id: str, **attributes: str) -> ExternalIdentity:
    """构造飞书身份。"""
    return ExternalIdentity(
        tenant_id=tenant, provider="feishu", subject=subject, user_id=user_id, attributes=attributes
    )


def _config(tenant: str, **kwargs: object) -> TenantIdentityConfig:
    """构造租户的飞书配置。"""
    fields: dict[str, object] = {
        "app_id": "cli_app",
        "app_secret_ref": "feishu-app-secret",
        "encrypt_key_ref": "feishu-encrypt-key",
        "verification_token_ref": "feishu-verification-token",
    }
    fields.update(kwargs)
    return TenantIdentityConfig(tenant_id=tenant, provider="feishu", **fields)  # type: ignore[arg-type]


def _login(tenant: str, token_hash: str, user_id: str, *, at: float = 100.0) -> LoginSession:
    """构造登录会话：有效 1000 秒。"""
    return LoginSession(
        token_hash=token_hash,
        tenant_id=tenant,
        user_id=user_id,
        created_at=at,
        expires_at=at + 1000,
        last_seen_at=at,
    )


def _attempt(state: str, tenant: str = "acme", *, at: float = 100.0) -> LoginAttempt:
    """构造登录尝试：有效 600 秒。"""
    return LoginAttempt(
        state=state,
        tenant_id=tenant,
        code_verifier="v" * 43,
        return_to="/console/",
        created_at=at,
        expires_at=at + 600,
    )


def test_implements_protocol(identity: IdentityStore) -> None:
    """实现满足协议。"""
    assert isinstance(identity, IdentityStore)


# --- 记录本身 ---


def test_new_user_id_format() -> None:
    """用户标识由 ``ids.new_id("usr")`` 生成：``usr_`` 加 24 位十六进制。"""
    generated = {new_user_id() for _ in range(50)}
    assert len(generated) == 50
    assert all(re.fullmatch(r"usr_[0-9a-f]{24}", user_id) for user_id in generated)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": "platform_admin"},
        {"status": "frozen"},
        {"status": "disabled"},
        {"status": "disabled", "disabled_reason": "admin"},
        {"status": "disabled", "disabled_reason": "bored", "disabled_at": 1.0},
        {"disabled_reason": "admin"},
        {"disabled_at": 1.0},
    ],
)
def test_user_record_rejects_inconsistent_fields(kwargs: dict[str, object]) -> None:
    """角色、状态取值受限；停用的必须带原因与时间，启用的不能带。"""
    with pytest.raises(ValueError):
        _user("acme", "usr_x", **kwargs)


def test_other_records_validate_fields() -> None:
    """身份源只认飞书；会话有效期为正数；分页大小有上下限。"""
    with pytest.raises(ValueError, match="provider"):
        ExternalIdentity("acme", "oidc", "sub", "usr_x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="session_ttl_seconds"):
        _config("acme", session_ttl_seconds=0)
    for limit in (0, 1001):
        with pytest.raises(ValueError, match="limit"):
            UserQuery(limit=limit)


# --- 用户 ---


async def test_user_create_get_and_conflict(identity: IdentityStore) -> None:
    """建、取；标识重复报冲突；别的租户拿着正确的标识也取不到。"""
    alice = _user("acme", "usr_a", avatar_url="https://example.test/a.png")
    await identity.create_user(alice)
    assert await identity.get_user("acme", "usr_a") == alice
    assert await identity.get_user("acme", "usr_missing") is None
    assert await identity.get_user("other", "usr_a") is None
    with pytest.raises(ConflictError):
        await identity.create_user(_user("acme", "usr_a"))


async def test_user_with_identity_is_created_atomically(identity: IdentityStore) -> None:
    """用户与外部身份一起建：外部身份冲突时用户也不建；按外部身份取得到用户。"""
    await identity.create_user(
        _user("acme", "usr_a"), identity=_identity("acme", "on_a", "usr_a", open_id="ou_a")
    )
    found = await identity.get_user_by_identity("acme", "feishu", "on_a")
    assert found is not None
    assert found.user_id == "usr_a"
    assert await identity.get_user_by_identity("acme", "feishu", "on_missing") is None
    assert await identity.get_user_by_identity("other", "feishu", "on_a") is None

    with pytest.raises(ConflictError):
        await identity.create_user(
            _user("acme", "usr_b"), identity=_identity("acme", "on_a", "usr_b")
        )
    assert await identity.get_user("acme", "usr_b") is None

    # 同一个外部标识在别的租户下是另一个人，互不冲突
    await identity.create_user(
        _user("other", "usr_c"), identity=_identity("other", "on_a", "usr_c")
    )
    other = await identity.get_user_by_identity("other", "feishu", "on_a")
    assert other is not None
    assert other.user_id == "usr_c"

    with pytest.raises(ValueError, match="user_id"):
        await identity.create_user(
            _user("acme", "usr_d"), identity=_identity("acme", "on_d", "usr_other")
        )
    with pytest.raises(ValueError, match="tenant_id"):
        await identity.create_user(
            _user("acme", "usr_d"), identity=_identity("other", "on_d", "usr_d")
        )


async def test_list_users_filters_and_pages(identity: IdentityStore) -> None:
    """按状态、角色、是否查不到过滤；按用户标识翻页不重不漏；只列本租户的。"""
    for index in range(5):
        await identity.create_user(_user("acme", f"usr_{index}"))
    await identity.create_user(_user("other", "usr_9"))
    await identity.set_user_role("acme", "usr_1", "tenant_admin")
    await identity.set_user_status("acme", "usr_2", "disabled", reason="admin", at=20.0)
    await identity.mark_user_unverified("acme", "usr_3", at=30.0)

    def ids(users: list[UserRecord]) -> list[str]:
        return [user.user_id for user in users]

    assert ids(await identity.list_users("acme", UserQuery())) == [f"usr_{i}" for i in range(5)]
    assert ids(await identity.list_users("acme", UserQuery(status="disabled"))) == ["usr_2"]
    active = await identity.list_users("acme", UserQuery(status="active"))
    assert ids(active) == ["usr_0", "usr_1", "usr_3", "usr_4"]
    assert ids(await identity.list_users("acme", UserQuery(role="tenant_admin"))) == ["usr_1"]
    assert ids(await identity.list_users("acme", UserQuery(unverified=True))) == ["usr_3"]
    verified = await identity.list_users("acme", UserQuery(unverified=False, status="active"))
    assert ids(verified) == ["usr_0", "usr_1", "usr_4"]
    assert ids(await identity.list_users("other", UserQuery())) == ["usr_9"]

    seen: list[str] = []
    after: str | None = None
    for _ in range(5):
        page = await identity.list_users("acme", UserQuery(after=after, limit=2))
        if not page:
            break
        seen.extend(ids(page))
        after = page[-1].user_id
    assert seen == [f"usr_{i}" for i in range(5)]


async def test_listing_order_is_by_code_point(identity: IdentityStore) -> None:
    """用户与外部身份按码点排序翻页（大小写、标点不按语言规则重排），两个实现一致。"""
    keys = ["usr_b", "usr-a", "usr_A", "usrz", "usr_a", "USR_c", "usr.a"]
    for index, key in enumerate(keys):
        await identity.create_user(
            _user("acme", key), identity=_identity("acme", f"on{key}", key, n=str(index))
        )
    users = await identity.list_users("acme", UserQuery())
    assert [user.user_id for user in users] == sorted(keys)
    page = await identity.list_users("acme", UserQuery(after="usr_", limit=3))
    assert [user.user_id for user in page] == [key for key in sorted(keys) if key > "usr_"][:3]
    subjects = await identity.list_external_identities("acme", "feishu", after_subject="onusr-a")
    assert [item.subject for item in subjects] == sorted(
        f"on{key}" for key in keys if f"on{key}" > "onusr-a"
    )


async def test_set_user_role(identity: IdentityStore) -> None:
    """改角色返回改后的记录；用户不存在或属于别的租户报不存在。"""
    await identity.create_user(_user("acme", "usr_a"))
    updated = await identity.set_user_role("acme", "usr_a", "tenant_admin")
    assert updated.role == "tenant_admin"
    assert await identity.get_user("acme", "usr_a") == updated
    with pytest.raises(NotFoundError):
        await identity.set_user_role("acme", "usr_missing", "tenant_admin")
    with pytest.raises(NotFoundError):
        await identity.set_user_role("other", "usr_a", "tenant_member")


async def test_set_user_status_reports_real_changes(identity: IdentityStore) -> None:
    """停用与启用返回状态是否真的变了；重复停用不改原因与时间；启用清掉原因与时间。"""
    await identity.create_user(_user("acme", "usr_a"))
    assert await identity.set_user_status(
        "acme", "usr_a", "disabled", reason="feishu_resigned", at=50.0
    )
    assert not await identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=60.0)
    disabled = await identity.get_user("acme", "usr_a")
    assert disabled is not None
    assert (disabled.status, disabled.disabled_reason, disabled.disabled_at) == (
        "disabled", "feishu_resigned", 50.0,
    )  # fmt: skip

    assert await identity.set_user_status("acme", "usr_a", "active", at=70.0)
    assert not await identity.set_user_status("acme", "usr_a", "active", at=80.0)
    enabled = await identity.get_user("acme", "usr_a")
    assert enabled is not None
    assert (enabled.status, enabled.disabled_reason, enabled.disabled_at) == ("active", None, None)

    with pytest.raises(NotFoundError):
        await identity.set_user_status("acme", "usr_missing", "active", at=1.0)
    with pytest.raises(NotFoundError):
        await identity.set_user_status("other", "usr_a", "disabled", reason="admin", at=1.0)
    with pytest.raises(ValueError, match="reason"):
        await identity.set_user_status("acme", "usr_a", "disabled", at=1.0)
    with pytest.raises(ValueError, match="reason"):
        await identity.set_user_status("acme", "usr_a", "active", reason="admin", at=1.0)


async def test_concurrent_disable_changes_once(identity: IdentityStore) -> None:
    """事件回调与对账同时停用同一人：恰好一次报告「变了」。"""
    await identity.create_user(_user("acme", "usr_a"))
    results = await asyncio.gather(
        *(
            identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=float(i))
            for i in range(8)
        )
    )
    assert results.count(True) == 1


async def test_keep_an_admin_refuses_removing_the_last_one(identity: IdentityStore) -> None:
    """要求保住管理员时，不能降级或停用最后一名启用的管理员；还有别的启用管理员就可以。"""
    await identity.create_user(_user("acme", "usr_a", role="tenant_admin"))
    await identity.create_user(_user("acme", "usr_b", role="tenant_admin"))
    await identity.create_user(_user("acme", "usr_m"))
    await identity.create_user(_user("other", "usr_o", role="tenant_admin"))
    await identity.set_user_status("acme", "usr_b", "disabled", reason="admin", at=1.0)

    # 启用的管理员只剩 usr_a（usr_b 已停用；别的租户的管理员不算）
    with pytest.raises(ConflictError):
        await identity.set_user_role("acme", "usr_a", "tenant_member", keep_an_admin=True)
    with pytest.raises(ConflictError):
        await identity.set_user_status(
            "acme", "usr_a", "disabled", reason="admin", at=2.0, keep_an_admin=True
        )
    unchanged = await identity.get_user("acme", "usr_a")
    assert unchanged is not None
    assert (unchanged.role, unchanged.status) == ("tenant_admin", "active")

    # 不涉及最后一名管理员的改动照常：改成员、给管理员再设一次管理员、停用已停用的
    member = await identity.set_user_role("acme", "usr_m", "tenant_member", keep_an_admin=True)
    assert member.role == "tenant_member"
    again = await identity.set_user_role("acme", "usr_a", "tenant_admin", keep_an_admin=True)
    assert again.role == "tenant_admin"
    assert not await identity.set_user_status(
        "acme", "usr_b", "disabled", reason="admin", at=3.0, keep_an_admin=True
    )
    with pytest.raises(NotFoundError):
        await identity.set_user_role("acme", "usr_missing", "tenant_member", keep_an_admin=True)

    # 再有一名启用的管理员，就可以降级 usr_a
    await identity.set_user_status("acme", "usr_b", "active", at=4.0)
    demoted = await identity.set_user_role("acme", "usr_a", "tenant_member", keep_an_admin=True)
    assert demoted.role == "tenant_member"
    # 不要求保住时，存储不拦（由服务层决定什么时候要求）
    assert await identity.set_user_status("acme", "usr_b", "disabled", reason="admin", at=5.0)


@pytest.mark.parametrize("change", ["role", "status", "mixed"])
async def test_mutual_demotion_keeps_one_admin(identity: IdentityStore, change: str) -> None:
    """两名管理员同时互相降级或停用：至多一个成功，租户里总留着一名启用的管理员。"""
    await identity.create_user(_user("acme", "usr_a", role="tenant_admin"))
    await identity.create_user(_user("acme", "usr_b", role="tenant_admin"))

    async def demote(user_id: str, by_status: bool) -> object:
        if by_status:
            return await identity.set_user_status(
                "acme", user_id, "disabled", reason="admin", at=1.0, keep_an_admin=True
            )
        return await identity.set_user_role("acme", user_id, "tenant_member", keep_an_admin=True)

    by_status = {"role": (False, False), "status": (True, True), "mixed": (False, True)}[change]
    results = await asyncio.gather(
        demote("usr_a", by_status[0]), demote("usr_b", by_status[1]), return_exceptions=True
    )
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert not any(
        isinstance(r, BaseException) and not isinstance(r, ConflictError) for r in results
    )
    admins = await identity.list_users("acme", UserQuery(role="tenant_admin", status="active"))
    assert len(admins) == 1


async def test_records_of_a_missing_tenant_are_refused(identity: IdentityStore) -> None:
    """租户不存在时，建记录的方法一律报不存在（租户以控制面为准）。"""
    with pytest.raises(NotFoundError):
        await identity.create_user(_user("ghost", "usr_g"))
    with pytest.raises(NotFoundError):
        await identity.create_user(
            _user("ghost", "usr_g"), identity=_identity("ghost", "on_g", "usr_g")
        )
    with pytest.raises(NotFoundError):
        await identity.put_identity_config(_config("ghost"))
    with pytest.raises(NotFoundError):
        await identity.create_login_attempt(_attempt("st_g", "ghost"))
    with pytest.raises(NotFoundError):
        await identity.add_user_usage(
            "ghost", "2026-10-01", "usr_g", input_tokens=1, output_tokens=1, total_tokens=2, turns=1
        )
    with pytest.raises(NotFoundError):
        await identity.claim_reconcile(
            "ghost", owner="w1", now=1.0, interval=0.0, lease_seconds=60.0
        )
    assert await identity.get_user("ghost", "usr_g") is None
    assert await identity.get_identity_config("ghost", "feishu") is None
    assert await identity.take_login_attempt("st_g", now=0.0) is None


async def test_update_user_profile(identity: IdentityStore) -> None:
    """刷新显示名与头像；给了登录时间才改登录时间。"""
    await identity.create_user(_user("acme", "usr_a", avatar_url="https://example.test/old.png"))
    renamed = await identity.update_user_profile(
        "acme", "usr_a", display_name="张三", avatar_url=None, last_login_at=99.0
    )
    assert (renamed.display_name, renamed.avatar_url, renamed.last_login_at) == ("张三", None, 99.0)
    kept = await identity.update_user_profile(
        "acme", "usr_a", display_name="张三丰", avatar_url="https://example.test/new.png"
    )
    assert (kept.display_name, kept.last_login_at) == ("张三丰", 99.0)
    assert await identity.get_user("acme", "usr_a") == kept
    with pytest.raises(NotFoundError):
        await identity.update_user_profile("other", "usr_a", display_name="x", avatar_url=None)


async def test_unverified_mark_and_clear(identity: IdentityStore) -> None:
    """标记查不到只记第一次的时间；清掉后可以重新标记；返回是否真的变了。"""
    await identity.create_user(_user("acme", "usr_a"))
    assert await identity.mark_user_unverified("acme", "usr_a", at=100.0)
    assert not await identity.mark_user_unverified("acme", "usr_a", at=200.0)
    marked = await identity.get_user("acme", "usr_a")
    assert marked is not None
    assert marked.unverified_since == 100.0

    assert await identity.clear_user_unverified("acme", "usr_a")
    assert not await identity.clear_user_unverified("acme", "usr_a")
    cleared = await identity.get_user("acme", "usr_a")
    assert cleared is not None
    assert cleared.unverified_since is None

    with pytest.raises(NotFoundError):
        await identity.mark_user_unverified("other", "usr_a", at=1.0)
    with pytest.raises(NotFoundError):
        await identity.clear_user_unverified("acme", "usr_missing")


# --- 外部身份 ---


async def test_external_identity_create_and_attributes(identity: IdentityStore) -> None:
    """单独建外部身份：重复报冲突、一人在同一身份源只能有一个、用户不存在报不存在；改属性限定
    在租户内；存进去的属性与调用方手里的字典互不牵连。"""
    await identity.create_user(_user("acme", "usr_a"))
    await identity.create_user(_user("acme", "usr_b"))
    attributes = {"open_id": "ou_1"}
    await identity.create_external_identity(
        ExternalIdentity("acme", "feishu", "on_a", "usr_a", attributes)
    )
    attributes["open_id"] = "被调用方改掉"
    [stored] = await identity.list_external_identities("acme", "feishu", user_id="usr_a")
    assert stored.attributes == {"open_id": "ou_1"}

    with pytest.raises(ConflictError):
        await identity.create_external_identity(_identity("acme", "on_a", "usr_b"))
    with pytest.raises(ConflictError):
        await identity.create_external_identity(_identity("acme", "on_a2", "usr_a"))
    assert await identity.get_user_by_identity("acme", "feishu", "on_a2") is None
    with pytest.raises(NotFoundError):
        await identity.create_external_identity(_identity("acme", "on_b", "usr_missing"))
    with pytest.raises(NotFoundError):
        await identity.create_external_identity(_identity("other", "on_c", "usr_a"))

    updated = await identity.update_identity_attributes(
        "acme", "feishu", "on_a", {"open_id": "ou_2", "备注": "中文 '引号'"}
    )
    assert updated == _identity("acme", "on_a", "usr_a", open_id="ou_2", 备注="中文 '引号'")
    listed = await identity.list_external_identities("acme", "feishu", user_id="usr_a")
    assert listed == [updated]
    with pytest.raises(NotFoundError):
        await identity.update_identity_attributes("other", "feishu", "on_a", {"open_id": "x"})
    with pytest.raises(NotFoundError):
        await identity.update_identity_attributes("acme", "feishu", "on_missing", {})


async def test_list_external_identities_for_reconcile(identity: IdentityStore) -> None:
    """对账取「启用且有飞书身份」的人：可按用户状态、用户过滤，按外部标识翻页。"""
    for index in range(5):
        user_id = f"usr_{index}"
        await identity.create_user(
            _user("acme", user_id), identity=_identity("acme", f"on_{index}", user_id)
        )
    await identity.create_user(_user("acme", "usr_nofeishu"))
    await identity.create_user(
        _user("other", "usr_9"), identity=_identity("other", "on_9", "usr_9")
    )
    await identity.set_user_status("acme", "usr_1", "disabled", reason="admin", at=1.0)

    def subjects(found: list[ExternalIdentity]) -> list[str]:
        return [item.subject for item in found]

    everyone = await identity.list_external_identities("acme", "feishu")
    assert subjects(everyone) == [f"on_{i}" for i in range(5)]
    assert {item.tenant_id for item in everyone} == {"acme"}
    active = await identity.list_external_identities("acme", "feishu", user_status="active")
    assert subjects(active) == ["on_0", "on_2", "on_3", "on_4"]
    by_user = await identity.list_external_identities("acme", "feishu", user_id="usr_3")
    assert [(item.subject, item.user_id) for item in by_user] == [("on_3", "usr_3")]

    seen: list[str] = []
    after: str | None = None
    for _ in range(5):
        page = await identity.list_external_identities(
            "acme", "feishu", user_status="active", after_subject=after, limit=3
        )
        if not page:
            break
        seen.extend(subjects(page))
        after = page[-1].subject
    assert seen == ["on_0", "on_2", "on_3", "on_4"]
    with pytest.raises(ValueError, match="limit"):
        await identity.list_external_identities("acme", "feishu", limit=0)


# --- 租户身份配置 ---


async def test_identity_config_put_get_list(identity: IdentityStore) -> None:
    """写入或替换配置（企业标识的绑定除外，见下一个用例）；按租户取，别的租户取不到；能列出
    配置了飞书的全部租户。"""
    assert await identity.get_identity_config("acme", "feishu") is None
    assert await identity.list_identity_configs("feishu") == []

    await identity.put_identity_config(_config("acme"))
    assert await identity.get_identity_config("acme", "feishu") == _config("acme")
    assert await identity.get_identity_config("other", "feishu") is None

    replaced = _config("acme", app_id="cli_new", session_ttl_seconds=3600)
    await identity.put_identity_config(replaced)
    assert await identity.get_identity_config("acme", "feishu") == replaced

    await identity.put_identity_config(_config("other"))
    listed = await identity.list_identity_configs("feishu")
    assert [config.tenant_id for config in listed] == ["acme", "other"]


async def test_bind_tenant_key_only_once(identity: IdentityStore) -> None:
    """飞书企业标识首次绑定后不再改；返回绑定后的值，由调用方比对；没有配置报不存在。"""
    await identity.put_identity_config(_config("acme"))
    await identity.put_identity_config(_config("other"))
    assert await identity.bind_tenant_key("acme", "feishu", "tk_first") == "tk_first"
    assert await identity.bind_tenant_key("acme", "feishu", "tk_second") == "tk_first"
    bound = await identity.get_identity_config("acme", "feishu")
    assert bound is not None
    assert bound.feishu_tenant_key == "tk_first"
    untouched = await identity.get_identity_config("other", "feishu")
    assert untouched is not None
    assert untouched.feishu_tenant_key is None

    winners = await asyncio.gather(
        *(identity.bind_tenant_key("other", "feishu", f"tk_{i}") for i in range(6))
    )
    assert len(set(winners)) == 1

    with pytest.raises(NotFoundError):
        await identity.bind_tenant_key("nobody", "feishu", "tk")


async def test_put_config_keeps_the_bound_tenant_key(identity: IdentityStore) -> None:
    """绑定之后再写配置，绑定仍在，也不能借机重绑；写配置时不能带企业标识。"""
    await identity.put_identity_config(_config("acme"))
    assert await identity.bind_tenant_key("acme", "feishu", "tk_real") == "tk_real"

    await identity.put_identity_config(_config("acme", app_id="cli_new", session_ttl_seconds=60))
    config = await identity.get_identity_config("acme", "feishu")
    assert config is not None
    assert (config.app_id, config.session_ttl_seconds, config.feishu_tenant_key) == (
        "cli_new", 60, "tk_real",
    )  # fmt: skip
    assert await identity.bind_tenant_key("acme", "feishu", "tk_attacker") == "tk_real"

    with pytest.raises(ValueError, match="bind_tenant_key"):
        await identity.put_identity_config(_config("acme", feishu_tenant_key="tk_attacker"))
    with pytest.raises(ValueError, match="bind_tenant_key"):
        await identity.put_identity_config(_config("other", feishu_tenant_key="tk_x"))
    assert await identity.get_identity_config("other", "feishu") is None


# --- 登录会话 ---


async def test_login_session_lifecycle(identity: IdentityStore) -> None:
    """建、按令牌摘要取（过期的取不到）、刷新最近使用时间、删一个。"""
    await identity.create_user(_user("acme", "usr_a"))
    session = _login("acme", "h1", "usr_a", at=100.0)
    await identity.create_login_session(session)
    with pytest.raises(ConflictError):
        await identity.create_login_session(_login("acme", "h1", "usr_a"))
    with pytest.raises(NotFoundError):
        await identity.create_login_session(_login("acme", "h2", "usr_missing"))
    with pytest.raises(NotFoundError):
        await identity.create_login_session(_login("other", "h3", "usr_a"))

    assert await identity.get_login_session("h1", now=500.0) == session
    assert await identity.get_login_session("h1", now=1100.0) is None
    assert await identity.get_login_session("missing", now=500.0) is None

    assert await identity.touch_login_session("acme", "h1", at=400.0)
    touched = await identity.get_login_session("h1", now=500.0)
    assert touched is not None
    assert touched.last_seen_at == 400.0
    assert not await identity.touch_login_session("acme", "missing", at=400.0)

    assert await identity.delete_login_session("acme", "h1")
    assert not await identity.delete_login_session("acme", "h1")
    assert await identity.get_login_session("h1", now=500.0) is None


async def test_login_sessions_of_one_user_are_deleted_together(identity: IdentityStore) -> None:
    """删某人的全部登录会话，不碰别人的；别的租户的身份删不掉也改不了。"""
    await identity.create_user(_user("acme", "usr_a"))
    await identity.create_user(_user("acme", "usr_b"))
    for token_hash, user_id in (("h1", "usr_a"), ("h2", "usr_a"), ("h3", "usr_b")):
        await identity.create_login_session(_login("acme", token_hash, user_id))

    assert await identity.delete_user_login_sessions("other", "usr_a") == 0
    assert not await identity.delete_login_session("other", "h1")
    assert not await identity.touch_login_session("other", "h1", at=900.0)
    kept = await identity.get_login_session("h1", now=500.0)
    assert kept is not None
    assert kept.last_seen_at == 100.0

    assert await identity.delete_user_login_sessions("acme", "usr_a") == 2
    assert await identity.delete_user_login_sessions("acme", "usr_a") == 0
    assert await identity.get_login_session("h1", now=500.0) is None
    assert await identity.get_login_session("h2", now=500.0) is None
    assert await identity.get_login_session("h3", now=500.0) is not None


# --- 登录尝试 ---


async def test_login_attempt_is_taken_once(identity: IdentityStore) -> None:
    """登录尝试取出即删，只能取一次；state 重复报冲突。"""
    attempt = _attempt("st_1")
    await identity.create_login_attempt(attempt)
    with pytest.raises(ConflictError):
        await identity.create_login_attempt(_attempt("st_1"))
    assert await identity.take_login_attempt("st_1", now=200.0) == attempt
    assert await identity.take_login_attempt("st_1", now=200.0) is None
    assert await identity.take_login_attempt("missing", now=200.0) is None


async def test_login_attempt_concurrent_take_has_one_winner(identity: IdentityStore) -> None:
    """同一个 state 被并发取多次（回调被重放）：只有一个拿到。"""
    await identity.create_login_attempt(_attempt("st_race"))
    taken = await asyncio.gather(
        *(identity.take_login_attempt("st_race", now=200.0) for _ in range(8))
    )
    assert sum(item is not None for item in taken) == 1


async def test_expired_login_attempt_is_not_returned(identity: IdentityStore) -> None:
    """过期的登录尝试取不到，而且这一取也把它删掉了。"""
    await identity.create_login_attempt(_attempt("st_old", at=100.0))
    assert await identity.take_login_attempt("st_old", now=700.0) is None
    assert await identity.take_login_attempt("st_old", now=200.0) is None


async def test_purge_expired_logins_across_tenants(identity: IdentityStore) -> None:
    """维护任务清掉全部租户里过期的登录会话与登录尝试（到期时刻等于 now 也算过期），别的不动。"""
    await identity.create_user(_user("acme", "usr_a"))
    await identity.create_user(_user("other", "usr_o"))
    # 登录会话有效 1000 秒：h_old、h_other 在 1100 到期，h_new 在 1900 到期
    await identity.create_login_session(_login("acme", "h_old", "usr_a", at=100.0))
    await identity.create_login_session(_login("other", "h_other", "usr_o", at=100.0))
    await identity.create_login_session(_login("acme", "h_new", "usr_a", at=900.0))
    # 登录尝试有效 600 秒：st_old、st_other 在 700 到期，st_new 在 1700 到期
    await identity.create_login_attempt(_attempt("st_old", "acme", at=100.0))
    await identity.create_login_attempt(_attempt("st_other", "other", at=100.0))
    await identity.create_login_attempt(_attempt("st_new", "acme", at=1100.0))

    assert await identity.purge_expired_logins(now=50.0) == 0
    assert await identity.purge_expired_logins(now=1100.0) == 4
    assert await identity.purge_expired_logins(now=1100.0) == 0

    # 清掉的就不在了：拿更早的 now 去取也取不到
    assert await identity.get_login_session("h_old", now=500.0) is None
    assert await identity.get_login_session("h_other", now=500.0) is None
    assert await identity.take_login_attempt("st_other", now=200.0) is None
    assert await identity.get_login_session("h_new", now=1100.0) is not None
    assert await identity.take_login_attempt("st_new", now=1200.0) is not None


# --- 按用户用量 ---


async def test_user_usage_accumulates_and_lists(identity: IdentityStore) -> None:
    """按租户、日期、用户累加；按日期区间（两端都含）列出，可只看一个人；租户之间隔离。"""
    first = await identity.add_user_usage(
        "acme", "2026-10-01", "usr_a", input_tokens=100, output_tokens=20, total_tokens=120, turns=1
    )
    assert (first.total_tokens, first.turns) == (120, 1)
    total = await identity.add_user_usage(
        "acme", "2026-10-01", "usr_a", input_tokens=50, output_tokens=5, total_tokens=55, turns=1
    )
    assert (total.input_tokens, total.output_tokens, total.total_tokens, total.turns) == (
        150, 25, 175, 2,
    )  # fmt: skip
    await identity.add_user_usage(
        "acme", "2026-10-01", "usr_b", input_tokens=1, output_tokens=1, total_tokens=2, turns=1
    )
    await identity.add_user_usage(
        "acme", "2026-10-02", "usr_a", input_tokens=3, output_tokens=3, total_tokens=6, turns=1
    )
    await identity.add_user_usage(
        "other", "2026-10-01", "usr_a", input_tokens=9, output_tokens=9, total_tokens=18, turns=3
    )

    rows = await identity.list_user_usage("acme", since="2026-10-01", until="2026-10-02")
    assert [(r.day, r.user_id, r.total_tokens) for r in rows] == [
        ("2026-10-01", "usr_a", 175), ("2026-10-01", "usr_b", 2), ("2026-10-02", "usr_a", 6),
    ]  # fmt: skip
    assert rows[0] == total
    mine = await identity.list_user_usage(
        "acme", since="2026-10-02", until="2026-10-31", user_id="usr_a"
    )
    assert [(r.day, r.total_tokens) for r in mine] == [("2026-10-02", 6)]
    theirs = await identity.list_user_usage("other", since="2026-10-01", until="2026-10-01")
    assert [(r.user_id, r.turns) for r in theirs] == [("usr_a", 3)]
    assert await identity.list_user_usage("acme", since="2026-11-01", until="2026-11-30") == []


async def test_concurrent_user_usage_is_not_lost(identity: IdentityStore) -> None:
    """并发累加不丢数。"""
    await asyncio.gather(
        *(
            identity.add_user_usage(
                "acme",
                "2026-10-01",
                "usr_a",
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                turns=1,
            )
            for _ in range(30)
        )
    )
    [row] = await identity.list_user_usage("acme", since="2026-10-01", until="2026-10-01")
    assert (row.input_tokens, row.output_tokens, row.total_tokens, row.turns) == (90, 60, 150, 30)


# --- 对账租约 ---


async def test_reconcile_lease_lifecycle(identity: IdentityStore) -> None:
    """认领、持有期间谁都认领不了（包括自己）、持有者完成、周期内不重复认领、到期再认领。"""
    assert await identity.get_reconcile_state("acme") is None
    claim = {"interval": 1000.0, "lease_seconds": 60.0}

    assert await identity.claim_reconcile("acme", owner="w1", now=100.0, **claim)
    assert await identity.get_reconcile_state("acme") == ReconcileState(
        tenant_id="acme", last_run_at=None, lease_owner="w1", lease_expires_at=160.0
    )
    assert not await identity.claim_reconcile("acme", owner="w2", now=110.0, **claim)
    assert not await identity.claim_reconcile("acme", owner="w1", now=110.0, **claim)
    # 别的租户的租约互不影响
    assert await identity.claim_reconcile("other", owner="w2", now=110.0, **claim)

    assert not await identity.finish_reconcile("acme", owner="w2", now=120.0)
    assert await identity.finish_reconcile("acme", owner="w1", now=130.0)
    assert not await identity.finish_reconcile("acme", owner="w1", now=131.0)
    assert await identity.get_reconcile_state("acme") == ReconcileState(
        tenant_id="acme", last_run_at=130.0, lease_owner=None, lease_expires_at=0.0
    )

    # 距上次完成不到一个周期：不认领；手工触发用 interval=0，不受周期限制
    assert not await identity.claim_reconcile("acme", owner="w2", now=1129.0, **claim)
    assert await identity.claim_reconcile(
        "acme", owner="w2", now=500.0, interval=0.0, lease_seconds=60.0
    )
    assert await identity.finish_reconcile("acme", owner="w2", now=510.0)
    assert await identity.claim_reconcile("acme", owner="w3", now=1510.0, **claim)


async def test_expired_reconcile_lease_can_be_taken_over(identity: IdentityStore) -> None:
    """持有者崩溃、租约到期：别人可以接手；原持有者事后不能再完成。"""
    claim = {"interval": 1000.0, "lease_seconds": 60.0}
    assert await identity.claim_reconcile("acme", owner="w1", now=100.0, **claim)
    assert not await identity.claim_reconcile("acme", owner="w2", now=159.0, **claim)
    assert await identity.claim_reconcile("acme", owner="w2", now=160.0, **claim)
    assert not await identity.finish_reconcile("acme", owner="w1", now=170.0)
    state = await identity.get_reconcile_state("acme")
    assert state is not None
    assert (state.lease_owner, state.last_run_at) == ("w2", None)


async def test_reconcile_claim_race_has_one_winner(identity: IdentityStore) -> None:
    """多个实例同时认领同一租户：恰好一个成功（首次认领与已有记录两种情形）。"""
    claim = {"now": 100.0, "interval": 1000.0, "lease_seconds": 60.0}
    first = await asyncio.gather(
        *(identity.claim_reconcile("acme", owner=f"w{i}", **claim) for i in range(8))
    )
    assert first.count(True) == 1

    later = {"now": 5000.0, "interval": 1000.0, "lease_seconds": 60.0}
    again = await asyncio.gather(
        *(identity.claim_reconcile("acme", owner=f"v{i}", **later) for i in range(8))
    )
    assert again.count(True) == 1


@pytest.mark.parametrize(("interval", "lease_seconds"), [(-1.0, 60.0), (0.0, 0.0), (0.0, -5.0)])
async def test_reconcile_claim_rejects_bad_window(
    identity: IdentityStore, interval: float, lease_seconds: float
) -> None:
    """周期不能为负，租约时长必须为正；参数不对时什么都不写。"""
    with pytest.raises(ValueError, match="interval|lease_seconds"):
        await identity.claim_reconcile(
            "acme", owner="w1", now=100.0, interval=interval, lease_seconds=lease_seconds
        )
    assert await identity.get_reconcile_state("acme") is None
