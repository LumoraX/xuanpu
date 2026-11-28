"""玄圃自有账号（ADR 0019）经 HTTP 的完整流程：管理员开通与签发设置链接、员工设口令、用户名口令
登录、失败不透露原因与锁定、改口令、防跨站检查，以及没有 ``public_url`` 时不可用。

夹具 ``identity_world`` 见本目录的 ``conftest.py``；口令摘要换成低成本参数，免得拖慢用例。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
from argon2 import PasswordHasher

from server.domains.identity.accounts import LOCK_AFTER_FAILURES, LOCK_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

WRITE = {"X-Requested-By": "xuanpu"}
PASSWORD = "correct horse battery"
REJECTED = {"error": {"code": "unauthenticated", "message": "用户名或口令不正确"}}


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def accounts_world(identity_world: Any) -> Any:
    """``identity_world`` 加上低成本的口令摘要参数。"""

    @asynccontextmanager
    async def opener(**kwargs: Any) -> AsyncIterator[Any]:
        async with identity_world(**kwargs) as ident:
            hashing = ident.world.platform.identity.accounts.hashing
            hashing.hasher = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
            yield ident

    return opener


async def post(ident: Any, path: str, body: dict[str, Any], **headers: str) -> httpx.Response:
    """发一个 POST，然后清掉 httpx 的 cookie 罐（cookie 一律由用例显式带）。"""
    response: httpx.Response = await ident.http.post(path, json=body, headers=headers)
    ident.http.cookies.clear()
    return response


async def create(ident: Any, username: str = "Zhang.San", **body: Any) -> dict[str, Any]:
    """管理员开通一名员工，返回响应体。"""
    payload = {"username": username, "display_name": "张三", **body}
    response = await post(ident, "/v1/users", payload, **bearer(ident.world.admin_key))
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def token_of(link: dict[str, Any]) -> str:
    """设置链接片段里的令牌。"""
    fragment = urlsplit(link["url"]).fragment
    key, _, value = fragment.partition("=")
    assert key == "password_setup"
    return value


async def setup(ident: Any, link: dict[str, Any], password: str = PASSWORD) -> httpx.Response:
    """兑现设置链接。"""
    return await post(
        ident, "/v1/auth/password/setup", {"token": token_of(link), "password": password}, **WRITE
    )


async def login(ident: Any, username: str, password: str, **extra: str) -> httpx.Response:
    """用户名口令登录。"""
    body = {"tenant": "acme", "username": username, "password": password}
    return await post(ident, "/v1/auth/password/login", body, **{**WRITE, **extra})


async def kinds(ident: Any) -> list[str]:
    """租户 acme 的审计记录种类，按时间顺序。"""
    records = await ident.world.platform.store.query_audit("acme", _all_audit())
    return [record.kind for record in records]


def _all_audit() -> Any:
    """不带条件的审计查询。"""
    from server.core.controlplane.models import AuditQuery

    return AuditQuery()


async def test_admin_creates_account_and_employee_sets_password(accounts_world: Any) -> None:
    """开通得到设置链接（令牌在片段里）；查看不消耗；兑现设口令并直接登录；链接只能用一次。"""
    async with accounts_world() as ident:
        created = await create(ident)
        link = created["setup_link"]
        assert created["user"]["display_name"] == "张三"
        assert link["username"] == "zhang.san" and link["purpose"] == "setup"
        assert link["url"].startswith("https://xuanpu.example.com/#password_setup=")
        body = {"token": token_of(link)}
        inspected = await post(ident, "/v1/auth/password/setup/inspect", body, **WRITE)
        assert inspected.json()["username"] == "zhang.san"
        assert (await setup(ident, link, "short")).status_code == 400
        done = await setup(ident, link)
        assert done.status_code == 200, done.text
        assert done.headers["cache-control"] == "no-store"
        token = ident.session_token(done)
        me = (await ident.http.get("/v1/me", headers=ident.cookie(token))).json()
        assert me["user_id"] == created["user"]["user_id"]
        again = await setup(ident, link)
        assert again.status_code == 401
        assert "user_created" in await kinds(ident)
        assert {"password_link_issued", "password_set", "login_succeeded"} <= set(await kinds(ident))


async def test_login_is_case_insensitive_and_hides_failure_reasons(accounts_world: Any) -> None:
    """用户名不区分大小写；无此人、口令错、租户不存在、员工停用对外是同一个 401。"""
    async with accounts_world() as ident:
        created = await create(ident)
        await setup(ident, created["setup_link"])
        ok = await login(ident, "ZHANG.SAN", PASSWORD)
        assert ok.status_code == 200 and ident.session_token(ok)
        for username, password in (("zhang.san", "wrong password!"), ("nobody", PASSWORD)):
            response = await login(ident, username, password)
            assert (response.status_code, response.json()) == (401, REJECTED)
        other = await post(
            ident, "/v1/auth/password/login",
            {"tenant": "missing", "username": "zhang.san", "password": PASSWORD}, **WRITE,
        )  # fmt: skip
        assert (other.status_code, other.json()) == (401, REJECTED)
        user_id = created["user"]["user_id"]
        disabled = await ident.http.put(
            f"/v1/users/{user_id}/status", json={"status": "disabled"},
            headers=bearer(ident.world.admin_key),
        )  # fmt: skip
        assert disabled.status_code == 200
        response = await login(ident, "zhang.san", PASSWORD)
        assert (response.status_code, response.json()) == (401, REJECTED)


async def test_repeated_failures_lock_the_account(accounts_world: Any) -> None:
    """连续失败到阈值锁定：锁定期内正确口令也被拒；到期后能登录，失败计数清零。"""
    async with accounts_world() as ident:
        await setup(ident, (await create(ident))["setup_link"])
        accounts = ident.world.platform.identity.accounts
        clock = [accounts._now()]
        accounts._now = lambda: clock[0]
        for _ in range(LOCK_AFTER_FAILURES):
            assert (await login(ident, "zhang.san", "wrong password!")).status_code == 401
        locked = await login(ident, "zhang.san", PASSWORD)
        assert (locked.status_code, locked.json()) == (401, REJECTED)
        clock[0] += LOCK_SECONDS + 1
        assert (await login(ident, "zhang.san", PASSWORD)).status_code == 200
        assert ident.world.platform.metrics.render().decode().count(
            'xuanpu_password_logins_total{result="locked"} 1.0'
        ) == 1


async def test_password_endpoints_require_csrf_headers(accounts_world: Any) -> None:
    """不带 ``X-Requested-By`` 或跨站 ``Origin`` 的登录与兑现请求被拒（403），不签发 cookie。"""
    async with accounts_world() as ident:
        created = await create(ident)
        await setup(ident, created["setup_link"])
        bare = await login(ident, "zhang.san", PASSWORD, **{"X-Requested-By": ""})
        assert bare.status_code == 403
        assert bare.json()["error"]["code"] == "csrf_rejected"
        cross = await login(ident, "zhang.san", PASSWORD, Origin="https://evil.example")
        assert cross.status_code == 403 and "set-cookie" not in cross.headers
        same = await login(ident, "zhang.san", PASSWORD, Origin="https://xuanpu.example.com")
        assert same.status_code == 200


async def test_admin_only_and_username_conflicts(accounts_world: Any) -> None:
    """成员不能开通；同一租户用户名不能重复（不区分大小写）；用户名不合规则 400。"""
    async with accounts_world() as ident:
        body = {"username": "li", "display_name": "李四"}
        member = await post(ident, "/v1/users", body, **bearer(ident.world.member_key))
        assert member.status_code == 403
        await create(ident, "li")
        duplicate = await post(ident, "/v1/users", {**body, "username": "LI"},
                               **bearer(ident.world.admin_key))  # fmt: skip
        assert duplicate.status_code == 409
        invalid = await post(ident, "/v1/users", {**body, "username": "李四"},
                             **bearer(ident.world.admin_key))  # fmt: skip
        assert invalid.status_code == 400


async def test_link_for_existing_user_and_reset(accounts_world: Any) -> None:
    """飞书来的员工补发链接要给用户名；已有口令的签发重置链接，兑现后旧登录全部作废。"""
    async with accounts_world() as ident:
        user = await ident.user(name="王五")
        old_session = await ident.login(user)
        path = f"/v1/users/{user.user_id}/password-link"
        admin = bearer(ident.world.admin_key)
        assert (await post(ident, path, {}, **admin)).status_code == 400
        first = await post(ident, path, {"username": "wangwu"}, **admin)
        assert first.json()["purpose"] == "setup"
        await setup(ident, first.json())
        reset = await post(ident, path, {}, **admin)
        assert reset.json()["purpose"] == "reset"
        renamed = await post(ident, path, {"username": "wang5"}, **admin)
        assert renamed.status_code == 400
        assert (await setup(ident, reset.json(), "a brand new secret")).status_code == 200
        stale = await ident.http.get("/v1/me", headers=ident.cookie(old_session))
        assert stale.status_code == 401
        assert (await login(ident, "wangwu", "a brand new secret")).status_code == 200


async def test_change_password_signs_out_other_sessions(accounts_world: Any) -> None:
    """改口令要核对旧口令；成功后旧 cookie 失效、换发新 cookie；API key 不能改口令。"""
    async with accounts_world() as ident:
        await setup(ident, (await create(ident))["setup_link"])
        first = ident.session_token(await login(ident, "zhang.san", PASSWORD))
        second = ident.session_token(await login(ident, "zhang.san", PASSWORD))
        wrong = {"current_password": "not my password", "new_password": "another long one"}
        headers = {**ident.cookie(first), **WRITE}
        assert (await post(ident, "/v1/me/password", wrong, **headers)).status_code == 401
        body = {"current_password": PASSWORD, "new_password": "another long one"}
        changed = await post(ident, "/v1/me/password", body, **headers)
        assert changed.status_code == 200, changed.text
        fresh = ident.session_token(changed)
        for token in (first, second):
            assert (await ident.http.get("/v1/me", headers=ident.cookie(token))).status_code == 401
        assert (await ident.http.get("/v1/me", headers=ident.cookie(fresh))).status_code == 200
        assert (await login(ident, "zhang.san", PASSWORD)).status_code == 401
        by_key = await post(ident, "/v1/me/password", body, **bearer(ident.world.admin_key))
        assert by_key.status_code == 400


async def test_disabled_user_gets_no_link(accounts_world: Any) -> None:
    """停用的员工不能签发设置链接；已签发的链接也兑现不了。"""
    async with accounts_world() as ident:
        created = await create(ident)
        user_id = created["user"]["user_id"]
        admin = bearer(ident.world.admin_key)
        await ident.http.put(f"/v1/users/{user_id}/status", json={"status": "disabled"},
                             headers=admin)  # fmt: skip
        assert (await setup(ident, created["setup_link"])).status_code == 401
        issued = await post(ident, f"/v1/users/{user_id}/password-link", {}, **admin)
        assert issued.status_code == 409


async def test_auth_methods_follow_configuration(accounts_world: Any) -> None:
    """登录方式随配置：配了 ``public_url`` 就有口令登录，配了飞书才有飞书。"""
    async with accounts_world() as ident:
        methods = (await ident.http.get("/v1/auth/methods", params={"tenant": "acme"})).json()
        assert methods == {"tenant": "acme", "password": True, "feishu": False}
        await ident.configure_feishu()
        methods = (await ident.http.get("/v1/auth/methods", params={"tenant": "acme"})).json()
        assert methods["feishu"] is True


async def test_password_login_needs_public_url(accounts_world: Any) -> None:
    """没有 ``public_url`` 时口令登录与开通都不可用（400）。"""
    async with accounts_world(public_url=None) as ident:
        methods = (await ident.http.get("/v1/auth/methods", params={"tenant": "acme"})).json()
        assert methods["password"] is False
        assert (await login(ident, "zhang.san", PASSWORD)).status_code == 400
        body = {"username": "zhang", "display_name": "张三"}
        refused = await post(ident, "/v1/users", body, **bearer(ident.world.admin_key))
        assert refused.status_code == 400
