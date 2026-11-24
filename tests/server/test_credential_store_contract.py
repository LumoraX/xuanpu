"""自有账号存储（``IdentityStore.credentials``）的契约测试：内存实现与 Postgres 实现各跑一遍。

存储不读时钟，过期与锁定都按用例显式传的 ``now`` 判断。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from server.core.errors import ConflictError, NotFoundError
from server.core.tenancy import Tenant
from server.domains.identity import UserRecord
from server.domains.identity.credential_models import (
    AccountToken,
    LocalCredential,
    normalize_username,
)

if TYPE_CHECKING:
    from server.core.controlplane import ControlPlaneStore
    from server.domains.identity import IdentityStore
    from server.domains.identity.credential_store import CredentialStore


@pytest.fixture
async def credentials(
    identity_stores: tuple[ControlPlaneStore, IdentityStore],
) -> CredentialStore:
    """凭据存储：租户 acme、other 各有员工 u1、u2。"""
    control, identity = identity_stores
    for tenant_id in ("acme", "other"):
        await control.create_tenant(Tenant(tenant_id=tenant_id, name=tenant_id, created_at=1.0))
        for user_id in ("usr_1", "usr_2"):
            await identity.create_user(
                UserRecord(tenant_id=tenant_id, user_id=user_id, display_name=user_id, created_at=1.0)
            )
    return identity.credentials


def _credential(tenant: str = "acme", user: str = "usr_1", name: str = "zhang") -> LocalCredential:
    """待设口令的凭据。"""
    return LocalCredential(tenant_id=tenant, user_id=user, username=name, created_at=10.0)


def _token(token_hash: str = "h1", *, tenant: str = "acme", user: str = "usr_1") -> AccountToken:
    """开通链接，100 时刻到期。"""
    return AccountToken(
        token_hash=token_hash, tenant_id=tenant, user_id=user, purpose="setup",
        created_at=10.0, expires_at=100.0, issued_by="key:admin",
    )  # fmt: skip


def test_username_is_normalized_and_restricted() -> None:
    """用户名去空白转小写；只收 ASCII 字母数字与 . _ -，首字符是字母或数字，长度 2–64。"""
    assert normalize_username("  Zhang.San ") == "zhang.san"
    for bad in ("a", "-abc", "张三", "a b", "ｚｈａｎｇ", "x" * 65, "a@b"):
        with pytest.raises(ValueError):
            normalize_username(bad)
    with pytest.raises(ValueError, match="规整"):
        LocalCredential(tenant_id="acme", user_id="u", username="Zhang", created_at=1.0)


async def test_create_and_find_are_tenant_scoped(credentials: CredentialStore) -> None:
    """按员工、按用户名都取得到；同名用户名在别的租户互不影响。"""
    await credentials.create_credential(_credential())
    await credentials.create_credential(_credential("other"))
    assert await credentials.get_credential("acme", "usr_1") == _credential()
    assert await credentials.find_credential("acme", "zhang") == _credential()
    assert await credentials.find_credential("other", "zhang") == _credential("other")
    assert await credentials.find_credential("acme", "nobody") is None
    assert await credentials.get_credential("acme", "usr_2") is None


async def test_create_conflicts_and_missing_user(credentials: CredentialStore) -> None:
    """同一员工开通两次、同一租户用户名重复都冲突；员工不存在报 404。"""
    await credentials.create_credential(_credential())
    with pytest.raises(ConflictError, match="已经开通"):
        await credentials.create_credential(_credential(name="other-name"))
    with pytest.raises(ConflictError, match="已被占用"):
        await credentials.create_credential(_credential(user="usr_2"))
    with pytest.raises(NotFoundError):
        await credentials.create_credential(_credential(user="usr_missing", name="ghost"))


async def test_failures_lock_and_success_clears(credentials: CredentialStore) -> None:
    """连续失败到阈值锁定并清零计数；登录成功解锁，``rehashed`` 换摘要但不改设口令时间。"""
    await credentials.create_credential(_credential())
    await credentials.set_password("acme", "usr_1", "hash-1", at=20.0)
    first = await credentials.record_login_failure(
        "acme", "usr_1", now=30.0, lock_after=3, lock_seconds=60.0
    )
    assert first is not None and (first.failed_attempts, first.locked_until) == (1, None)
    await credentials.record_login_failure("acme", "usr_1", now=31.0, lock_after=3, lock_seconds=60.0)
    locked = await credentials.record_login_failure(
        "acme", "usr_1", now=32.0, lock_after=3, lock_seconds=60.0
    )
    assert locked is not None and (locked.failed_attempts, locked.locked_until) == (0, 92.0)
    assert locked.locked(91.0) and not locked.locked(92.0)
    await credentials.record_login_success("acme", "usr_1", rehashed="hash-2")
    after = await credentials.get_credential("acme", "usr_1")
    assert after is not None
    assert (after.failed_attempts, after.locked_until, after.password_hash) == (0, None, "hash-2")
    assert after.password_changed_at == 20.0
    missing = await credentials.record_login_failure(
        "other", "usr_1", now=1.0, lock_after=3, lock_seconds=1.0
    )
    assert missing is None


async def test_concurrent_failures_are_all_counted(credentials: CredentialStore) -> None:
    """并发失败不会少记：4 次并发失败、阈值 10，计数正好是 4。"""
    await credentials.create_credential(_credential())
    await asyncio.gather(*(
        credentials.record_login_failure("acme", "usr_1", now=1.0, lock_after=10, lock_seconds=1.0)
        for _ in range(4)
    ))  # fmt: skip
    current = await credentials.get_credential("acme", "usr_1")
    assert current is not None and current.failed_attempts == 4


async def test_tokens_replace_expire_and_redeem_once(credentials: CredentialStore) -> None:
    """每名员工只有一条链接；过期取不到；兑现设口令并作废链接，第二次兑现失败。"""
    await credentials.create_credential(_credential())
    await credentials.put_account_token(_token("h1"))
    await credentials.put_account_token(_token("h2"))
    assert await credentials.get_account_token("h1", now=50.0) is None
    assert await credentials.get_account_token("h2", now=50.0) == _token("h2")
    assert await credentials.get_account_token("h2", now=100.0) is None
    assert await credentials.redeem_account_token(_token("h2"), "hash", at=60.0)
    assert not await credentials.redeem_account_token(_token("h2"), "hash-again", at=61.0)
    current = await credentials.get_credential("acme", "usr_1")
    assert current is not None
    assert (current.password_hash, current.password_changed_at) == ("hash", 60.0)


async def test_redeem_races_and_stale_tokens(credentials: CredentialStore) -> None:
    """同一条链接并发兑现至多一个成功；被替换的旧记录兑现不了。"""
    await credentials.create_credential(_credential())
    await credentials.put_account_token(_token("h1"))
    results = await asyncio.gather(*(
        credentials.redeem_account_token(_token("h1"), f"hash-{n}", at=60.0) for n in range(3)
    ))  # fmt: skip
    assert sorted(results) == [False, False, True]
    await credentials.put_account_token(_token("h3"))
    stale = replace(_token("h3"), purpose="reset")
    assert not await credentials.redeem_account_token(stale, "x", at=70.0)
    assert await credentials.get_account_token("h3", now=70.0) == _token("h3")


async def test_redeem_without_credential_keeps_token(credentials: CredentialStore) -> None:
    """员工没有凭据时兑现失败，链接保留。"""
    await credentials.put_account_token(_token("h1", user="usr_2"))
    assert not await credentials.redeem_account_token(_token("h1", user="usr_2"), "x", at=20.0)
    assert await credentials.get_account_token("h1", now=20.0) is not None


async def test_set_password_drops_tokens_and_purge(credentials: CredentialStore) -> None:
    """设口令删掉这名员工的链接；周期清理跨租户删过期链接；没有凭据的设口令返回 False。"""
    await credentials.create_credential(_credential())
    await credentials.put_account_token(_token("h1"))
    assert await credentials.set_password("acme", "usr_1", "hash", at=20.0)
    assert await credentials.get_account_token("h1", now=20.0) is None
    assert not await credentials.set_password("acme", "usr_2", "hash", at=20.0)
    await credentials.put_account_token(_token("h4", user="usr_2"))
    await credentials.put_account_token(_token("h5", tenant="other"))
    assert await credentials.purge_expired_account_tokens(99.0) == 0
    assert await credentials.purge_expired_account_tokens(100.0) == 2
    with pytest.raises(NotFoundError):
        await credentials.put_account_token(_token("h6", user="usr_missing"))
