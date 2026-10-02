"""飞书登录全流程（设计 7.1，另有加固）：开始、回调、七种失败原因、开放跳转、登录尝试 cookie、
并发首登、退出登录，以及登录结果的指标。

经 HTTP 走：平台的飞书客户端接 ``identity_world`` 的模拟飞书（本目录 ``conftest.py``）。浏览器的
cookie 由用例自己从 ``Set-Cookie`` 里取、显式带上（每次请求后清掉 httpx 的 cookie 罐），这样
「浏览器带了什么」一目了然。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from xuanpu_server.identity import TenantIdentityConfig, UserQuery
from xuanpu_server.identity.cookies import (
    LOGIN_ATTEMPT_COOKIE,
    SECURE_LOGIN_ATTEMPT_COOKIE,
    login_attempt_cookie_name,
)
from xuanpu_server.identity.login import with_login_error
from xuanpu_server.platform import ROOT_PRINCIPAL

ZHANG = {"union_id": "on_zhang", "open_id": "ou_zhang", "name": "张三"}
LI = {"union_id": "on_li", "open_id": "ou_li", "name": "李四"}
LANDING = "/console/"
WRITE = {"X-Requested-By": "xuanpu"}


# --- 浏览器的几个动作 ---


def set_cookies(response: httpx.Response) -> dict[str, str]:
    """响应里的 ``Set-Cookie``：cookie 名 → 整行。"""
    return {header.split("=", 1)[0]: header for header in response.headers.get_list("set-cookie")}


def cookie_value(response: httpx.Response, name: str) -> str:
    """响应写入的某个 cookie 的取值。"""
    return set_cookies(response)[name].split(";", 1)[0].split("=", 1)[1]


def attributes(header: str) -> set[str]:
    """一行 ``Set-Cookie`` 里除取值之外的属性（小写）。"""
    return {part.strip().lower() for part in header.split(";")[1:]}


def login_error(response: httpx.Response) -> str | None:
    """跳回落地页的 ``login_error``；不是跳回落地页带原因的返回 None。"""
    parts = urlsplit(response.headers["location"])
    return parse_qs(parts.query).get("login_error", [None])[0]


async def get(ident: Any, path: str, **kwargs: Any) -> httpx.Response:
    """发一个 GET，然后清掉 httpx 的 cookie 罐（cookie 一律由用例显式带）。"""
    response: httpx.Response = await ident.http.get(path, **kwargs)
    ident.http.cookies.clear()
    return response


async def begin(ident: Any, **params: str) -> httpx.Response:
    """开始登录。"""
    return await get(ident, "/v1/auth/feishu/login", params=params)


async def callback(
    ident: Any, *, binding: str | None, cookie_name: str | None = None, **params: str
) -> httpx.Response:
    """飞书跳回平台；``binding`` 是浏览器带的登录尝试 cookie（None 是没带）。"""
    name = cookie_name or login_attempt_cookie_name(ident.secure)
    headers = {"Cookie": f"{name}={binding}"} if binding is not None else {}
    return await get(ident, "/v1/auth/feishu/callback", params=params, headers=headers)


async def authorize(ident: Any, person: dict[str, str], **params: str) -> tuple[str, str, str]:
    """开始登录并在飞书授权页点同意，返回 ``(state, code, 登录尝试 cookie)``。"""
    started = await begin(ident, **{"tenant": "acme", **params})
    assert started.status_code == 302, started.text
    binding = cookie_value(started, login_attempt_cookie_name(ident.secure))
    state, code = ident.feishu.authorize(started.headers["location"], ident.feishu.person(**person))
    return state, code, binding


async def login(ident: Any, person: dict[str, str], **params: str) -> httpx.Response:
    """完整走一遍登录，返回回调的响应。"""
    state, code, binding = await authorize(ident, person, **params)
    return await callback(ident, binding=binding, state=state, code=code)


def session_token(ident: Any, response: httpx.Response) -> str:
    """回调响应写入的登录令牌。"""
    return cookie_value(response, ident.cookie_name)


def logins(ident: Any) -> dict[str, float]:
    """``xuanpu_logins_total`` 各个 ``result`` 的计数。"""
    text = ident.world.platform.metrics.render().decode()
    return {
        sample.labels["result"]: sample.value
        for family in text_string_to_metric_families(text)
        if family.name == "xuanpu_logins"
        for sample in family.samples
    }


async def assert_failed(
    ident: Any, response: httpx.Response, reason: str, *, audited: bool = True
) -> None:
    """跳回落地页并带上原因；没有签发登录 cookie；计了一次失败；按需核对 ``login_failed`` 审计。"""
    assert response.status_code == 302
    assert response.headers["location"] == f"{LANDING}?login_error={reason}"
    assert response.headers["cache-control"] == "no-store"
    assert ident.cookie_name not in set_cookies(response)
    assert logins(ident)[reason] >= 1
    records = await ident.audit("login_failed")
    if audited:
        assert records[-1].data == {"reason": reason}
        assert records[-1].source == "control"
    else:
        assert records == []


def attempt_cleared(ident: Any, response: httpx.Response) -> bool:
    """回调的响应清掉了登录尝试 cookie。"""
    name = login_attempt_cookie_name(ident.secure)
    header = set_cookies(response).get(name, "")
    return header.startswith(f'{name}="";') and "max-age=0" in attributes(header)


def test_login_error_keeps_the_landing_query_and_fragment() -> None:
    """落地页本来带查询串或片段的，login_error 加在查询串末尾、片段之前。"""
    assert with_login_error("/console/", "state_invalid") == "/console/?login_error=state_invalid"
    assert (
        with_login_error("/app?x=1#top", "user_disabled")
        == "/app?x=1&login_error=user_disabled#top"
    )


# --- 成功 ---


@pytest.mark.parametrize("secure", [True, False])
async def test_first_login_creates_a_member(identity_world: Any, secure: bool) -> None:
    """首次登录：开始时写登录尝试 cookie、跳去飞书授权页；回调时建普通成员（外部身份存
    union_id 与 open_id）、绑定飞书企业、写登录 cookie、清掉登录尝试 cookie、跳回 return_to；
    记 user_created 与 login_succeeded。cookie 名随 cookie_secure。"""
    local = "http://127.0.0.1:8420"
    mode = {} if secure else {"cookie_secure": False, "public_url": local}
    async with identity_world(**mode) as ident:
        await ident.configure_feishu()
        started = await begin(ident, tenant="acme", return_to="/console/sessions")
        assert started.status_code == 302
        assert started.headers["cache-control"] == "no-store"
        name = SECURE_LOGIN_ATTEMPT_COOKIE if secure else LOGIN_ATTEMPT_COOKIE
        flags = {"secure"} if secure else set()
        expected = {"path=/", "httponly", "samesite=lax", "max-age=600"} | flags
        assert attributes(set_cookies(started)[name]) == expected
        query = parse_qs(urlsplit(started.headers["location"]).query)
        base = "https://xuanpu.example.com" if secure else local
        assert query["redirect_uri"] == [f"{base}/v1/auth/feishu/callback"]

        person = ident.feishu.person(**ZHANG)
        state, code = ident.feishu.authorize(started.headers["location"], person)
        done = await callback(ident, binding=cookie_value(started, name), state=state, code=code)
        assert done.status_code == 302
        assert done.headers["location"] == "/console/sessions"
        expected = {"path=/", "httponly", "samesite=lax", "max-age=604800"} | flags
        assert attributes(set_cookies(done)[ident.cookie_name]) == expected
        assert attempt_cleared(ident, done)

        me = await get(ident, "/v1/me", headers=ident.cookie(session_token(ident, done)))
        user = await ident.store.get_user_by_identity("acme", "feishu", "on_zhang")
        assert me.json()["user_id"] == user.user_id
        assert (me.json()["display_name"], me.json()["role"]) == ("张三", "tenant_member")
        assert user.last_login_at is not None
        assert user.avatar_url == "https://avatar.example/a.png"
        (identity,) = await ident.store.list_external_identities(
            "acme", "feishu", user_id=user.user_id
        )
        assert identity.attributes == {"open_id": "ou_zhang"}
        config = await ident.store.get_identity_config("acme", "feishu")
        assert config.feishu_tenant_key == "tk_acme"
        for kind in ("user_created", "login_succeeded"):
            (record,) = await ident.audit(kind)
            assert (record.actor, record.user_id) == (f"user:{user.user_id}", user.user_id)
        assert logins(ident)["succeeded"] == 1


async def test_second_login_refreshes_the_profile(identity_world: Any) -> None:
    """再次登录：还是同一个用户，显示名、头像、open_id、最近登录时间刷新；每次登录签发新令牌，
    两个登录会话各自有效；只建过一次账号。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        first = await login(ident, ZHANG)
        before = await ident.store.get_user_by_identity("acme", "feishu", "on_zhang")
        changed = {**ZHANG, "name": "张三丰", "open_id": "ou_zhang_2"}
        second = await login(ident, {**changed, "avatar_url": "https://avatar.example/b.png"})
        assert second.headers["location"] == LANDING
        after = await ident.store.get_user_by_identity("acme", "feishu", "on_zhang")
        assert after.user_id == before.user_id
        assert (after.display_name, after.avatar_url) == ("张三丰", "https://avatar.example/b.png")
        assert after.last_login_at >= before.last_login_at
        (identity,) = await ident.store.list_external_identities(
            "acme", "feishu", user_id=after.user_id
        )
        assert identity.attributes == {"open_id": "ou_zhang_2"}
        tokens = {session_token(ident, first), session_token(ident, second)}
        assert len(tokens) == 2
        for token in tokens:
            me = await get(ident, "/v1/me", headers=ident.cookie(token))
            assert me.json()["display_name"] == "张三丰"
        assert len(await ident.audit("user_created")) == 1
        assert len(await ident.audit("login_succeeded")) == 2


async def test_session_ttl_follows_the_tenant_config(identity_world: Any) -> None:
    """租户的飞书配置给了 session_ttl_seconds 时，cookie 与登录会话都按它，不按平台默认。"""
    async with identity_world() as ident:
        await ident.configure_feishu(session_ttl_seconds=3600)
        done = await login(ident, ZHANG)
        assert "max-age=3600" in attributes(set_cookies(done)[ident.cookie_name])


@pytest.mark.parametrize(
    ("return_to", "landed"),
    [
        ("/console/sessions?id=1#top", "/console/sessions?id=1#top"),
        ("/", "/"),
        (None, LANDING),
        ("//evil.com", LANDING),
        ("/\\evil.com", LANDING),
        ("https://evil.com", LANDING),
        ("evil.com", LANDING),
        ("/console/\r\nSet-Cookie: x=1", LANDING),
        ("/" + "a" * 3000, LANDING),
    ],
)
async def test_return_to_only_accepts_site_paths(
    identity_world: Any, return_to: str | None, landed: str
) -> None:
    """return_to 只收站内路径；别的（含开放跳转）一律改用 identity.login_landing_path。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        params = {} if return_to is None else {"return_to": return_to}
        done = await login(ident, ZHANG, **params)
        assert done.headers["location"] == landed


async def test_tenant_defaults_to_identity_default_tenant(identity_world: Any) -> None:
    """省略 tenant 时用 identity.default_tenant。"""
    async with identity_world(default_tenant="acme") as ident:
        await ident.configure_feishu()
        started = await begin(ident)
        assert started.status_code == 302
        assert started.headers["location"].startswith("https://accounts.feishu.cn/")


# --- 七种失败原因 ---


async def test_feishu_denied(identity_world: Any) -> None:
    """员工在飞书授权页点了拒绝（error=access_denied）：feishu_denied，不调飞书接口。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        state, _, binding = await authorize(ident, ZHANG)
        denied = await callback(ident, binding=binding, state=state, error="access_denied")
        await assert_failed(ident, denied, "feishu_denied")
        assert attempt_cleared(ident, denied)
        (record,) = await ident.audit("login_failed")
        assert (record.actor, record.user_id) == ("anonymous", None)
        assert ident.feishu.requests == []


async def test_not_in_app_scope(identity_world: Any) -> None:
    """换令牌时飞书报 20010（不在应用的可用范围内）：not_in_app_scope，不建账号。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        body = {"code": 20010, "error": "unauthorized_client"}
        ident.feishu.fail["token"] = httpx.Response(400, json=body)
        await assert_failed(ident, await login(ident, ZHANG), "not_in_app_scope")
        assert await ident.store.get_user_by_identity("acme", "feishu", "on_zhang") is None


async def test_unknown_or_missing_state(identity_world: Any) -> None:
    """state 不存在或没带：state_invalid。不知道是哪个租户，不写审计，只计数。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        unknown = await callback(ident, binding="x", state="no-such-state", code="c")
        await assert_failed(ident, unknown, "state_invalid", audited=False)
        missing = await callback(ident, binding="x", code="c")
        await assert_failed(ident, missing, "state_invalid", audited=False)
        assert logins(ident)["state_invalid"] == 2
        assert attempt_cleared(ident, missing)


async def test_state_can_be_used_only_once(identity_world: Any) -> None:
    """同一个回调链接再用一次：state_invalid（登录尝试取出即删）。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        state, code, binding = await authorize(ident, ZHANG)
        first = await callback(ident, binding=binding, state=state, code=code)
        assert first.headers["location"] == LANDING
        replay = await callback(ident, binding=binding, state=state, code=code)
        await assert_failed(ident, replay, "state_invalid", audited=False)


@pytest.mark.parametrize("presented", ["missing", "forged", "duplicated", "other_name"])
async def test_login_attempt_cookie_must_match(identity_world: Any, presented: str) -> None:
    """登录尝试 cookie 缺失、不符、同名不止一个、名字不对：state_invalid（防登录伪造：别人的回调
    链接在员工的浏览器里用不了）。不拿授权码去换令牌；登录尝试已经作废，带对的 cookie 再来也不行。
    """
    async with identity_world() as ident:
        await ident.configure_feishu()
        state, code, binding = await authorize(ident, ZHANG)
        name = login_attempt_cookie_name(ident.secure)
        cookies = {
            "missing": None,
            "forged": f"{name}=forged-value",
            "duplicated": f"{name}={binding}; {name}={binding}",
            "other_name": f"{LOGIN_ATTEMPT_COOKIE}={binding}",
        }[presented]
        headers = {"Cookie": cookies} if cookies is not None else {}
        params = {"state": state, "code": code}
        response = await get(ident, "/v1/auth/feishu/callback", params=params, headers=headers)
        await assert_failed(ident, response, "state_invalid")
        assert ident.feishu.requests == []
        retry = await callback(ident, binding=binding, state=state, code=code)
        assert login_error(retry) == "state_invalid"
        assert await ident.store.get_user_by_identity("acme", "feishu", "on_zhang") is None


async def test_tenant_mismatch(identity_world: Any) -> None:
    """首次成功登录绑定飞书企业；之后别的飞书企业的员工来登录：tenant_mismatch，绑定不变。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        assert (await login(ident, ZHANG)).headers["location"] == LANDING
        other = await login(ident, {**LI, "tenant_key": "tk_other"})
        await assert_failed(ident, other, "tenant_mismatch")
        assert await ident.store.get_user_by_identity("acme", "feishu", "on_li") is None
        config = await ident.store.get_identity_config("acme", "feishu")
        assert config.feishu_tenant_key == "tk_acme"


@pytest.mark.parametrize("case", ["not_configured", "suspended", "ghost", "malformed"])
async def test_tenant_unavailable_at_start(identity_world: Any, case: str) -> None:
    """开始登录时租户没配飞书、已停用、不存在或标识不合法：tenant_unavailable。租户存在的记审计，
    不存在的（含标识不合法）不记。"""
    async with identity_world() as ident:
        platform = ident.world.platform
        if case != "not_configured":
            await ident.configure_feishu()
        if case == "suspended":
            await platform.set_tenant_status(ROOT_PRINCIPAL, "acme", "suspended")
        tenant = {"ghost": "ghost-co", "malformed": "Bad Tenant!"}.get(case, "acme")
        started = await begin(ident, tenant=tenant)
        audited = case in ("not_configured", "suspended")
        await assert_failed(ident, started, "tenant_unavailable", audited=audited)
        assert login_attempt_cookie_name(ident.secure) not in set_cookies(started)


async def test_tenant_unavailable_without_tenant_or_default(identity_world: Any) -> None:
    """省略 tenant、也没配 identity.default_tenant：tenant_unavailable。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        await assert_failed(ident, await begin(ident), "tenant_unavailable", audited=False)


async def test_tenant_suspended_during_login(identity_world: Any) -> None:
    """跳去飞书之后租户被停用：回调时 tenant_unavailable，不拿授权码换令牌。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        state, code, binding = await authorize(ident, ZHANG)
        await ident.world.platform.set_tenant_status(ROOT_PRINCIPAL, "acme", "suspended")
        response = await callback(ident, binding=binding, state=state, code=code)
        await assert_failed(ident, response, "tenant_unavailable")
        assert ident.feishu.requests == []


async def test_login_needs_public_url(identity_world: Any) -> None:
    """没配 public_url：开始登录就是 tenant_unavailable，不写登录尝试 cookie。"""
    async with identity_world(public_url=None) as ident:
        # 配置接口要求 public_url（见 test_identity_feishu_config），这里直接写存储
        await ident.store.put_identity_config(
            TenantIdentityConfig(
                tenant_id="acme",
                provider="feishu",
                app_id="cli_test_app",
                app_secret_ref="feishu-app-secret",
                encrypt_key_ref="feishu-encrypt-key",
                verification_token_ref="feishu-verification-token",
            )
        )
        started = await begin(ident, tenant="acme")
        await assert_failed(ident, started, "tenant_unavailable")
        assert set_cookies(started) == {}


async def test_user_disabled(identity_world: Any) -> None:
    """平台里已停用的员工再登录：user_disabled，审计带上他；不签发登录 cookie。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        await login(ident, ZHANG)
        user = await ident.store.get_user_by_identity("acme", "feishu", "on_zhang")
        await ident.store.set_user_status("acme", user.user_id, "disabled", reason="admin", at=1.0)
        await assert_failed(ident, await login(ident, ZHANG), "user_disabled")
        (record,) = await ident.audit("login_failed")
        assert (record.actor, record.user_id) == (f"user:{user.user_id}", user.user_id)


@pytest.mark.parametrize(
    "case", ["token_5xx", "token_network", "user_info_5xx", "no_code", "other_error"]
)
async def test_identity_unavailable(
    identity_world: Any, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    """飞书接口出错（5xx、网络错误）、回调既没有 code 也不是员工拒绝：identity_unavailable。
    日志与审计里没有授权码、App Secret 与飞书令牌。"""
    caplog.set_level(logging.DEBUG)
    async with identity_world() as ident:
        await ident.configure_feishu()
        failure: httpx.Response | Exception = httpx.Response(503, text="busy")
        if case == "token_network":
            failure = httpx.ConnectError("connection refused")
        if case in ("token_5xx", "token_network"):
            ident.feishu.fail["token"] = failure
        if case == "user_info_5xx":
            ident.feishu.fail["user_info"] = failure
        state, code, binding = await authorize(ident, ZHANG)
        params = {"state": state, "code": code}
        if case == "no_code":
            params = {"state": state}
        if case == "other_error":
            params = {"state": state, "error": "server_error"}
        response = await callback(ident, binding=binding, **params)
        await assert_failed(ident, response, "identity_unavailable")
        secrets = [code, binding, "feishu-app-secret-PLAINTEXT", *ident.feishu.tokens]
        records = [str(record.data) for record in await ident.audit("login_failed")]
        # 测试客户端扮演浏览器，它自己的请求日志里当然有回调地址（含授权码），那不是平台的日志
        platform_logs = "\n".join(
            record.getMessage()
            for record in caplog.records
            if not (record.name == "httpx" and "xuanpu.example.com" in record.getMessage())
        )
        assert "飞书登录失败（租户 acme）：identity_unavailable" in platform_logs
        for secret in secrets:
            assert secret not in platform_logs
            assert all(secret not in record for record in records)


# --- 并发首登 ---


async def test_concurrent_first_logins_create_one_user(
    identity_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一人同时开两个标签页首次登录：两次都在对方建账号之前查过「没有这个人」，也只建出一个
    用户（后到的建账号因外部身份冲突失败、重查到先建的那个）；两次登录都成功。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        store = ident.store
        original = store.get_user_by_identity
        barrier = asyncio.Barrier(2)
        calls = 0

        async def racing(*args: Any) -> Any:
            """头两次查询都查完再一起往下走：两边都以为这是首次登录。"""
            nonlocal calls
            calls += 1
            found = await original(*args)
            if calls <= 2:
                await asyncio.wait_for(barrier.wait(), timeout=5)
            return found

        monkeypatch.setattr(store, "get_user_by_identity", racing)
        flows = [await authorize(ident, ZHANG) for _ in range(2)]
        done = await asyncio.gather(
            *(callback(ident, binding=b, state=s, code=c) for s, c, b in flows)
        )
        assert [response.headers["location"] for response in done] == [LANDING, LANDING]
        assert calls >= 3  # 后到的那次冲突之后重查过
        (user,) = await store.list_users("acme", UserQuery())
        assert len(await ident.audit("user_created")) == 1
        for response in done:
            me = await get(ident, "/v1/me", headers=ident.cookie(session_token(ident, response)))
            assert me.json()["user_id"] == user.user_id


# --- 退出登录 ---


async def test_logout_deletes_the_session_and_clears_the_cookie(identity_world: Any) -> None:
    """退出登录：删掉当前登录会话（同一员工的别的会话不动）、清 cookie、记 logout。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        token = session_token(ident, await login(ident, ZHANG))
        other = session_token(ident, await login(ident, ZHANG))
        response = await ident.http.post(
            "/v1/auth/logout", headers={**ident.cookie(token), **WRITE}
        )
        assert response.status_code == 200
        assert response.json() == {"logged_out": True}
        assert ident.cleared(response)
        assert (await get(ident, "/v1/me", headers=ident.cookie(token))).status_code == 401
        assert (await get(ident, "/v1/me", headers=ident.cookie(other))).status_code == 200
        user = await ident.store.get_user_by_identity("acme", "feishu", "on_zhang")
        (record,) = await ident.audit("logout")
        assert (record.actor, record.user_id) == (f"user:{user.user_id}", user.user_id)


async def test_logout_rules(identity_world: Any) -> None:
    """退出登录是写请求，要过防跨站请求伪造的检查；只给浏览器登录用，API key 调是 400；
    没有凭据是 401。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        token = session_token(ident, await login(ident, ZHANG))
        forged = await ident.http.post("/v1/auth/logout", headers=ident.cookie(token))
        assert forged.json()["error"]["code"] == "csrf_rejected"
        key = {"Authorization": f"Bearer {ident.world.admin_key}"}
        with_key = await ident.http.post("/v1/auth/logout", headers=key)
        assert with_key.status_code == 400
        assert with_key.json()["error"]["code"] == "invalid_request"
        assert (await ident.http.post("/v1/auth/logout")).status_code == 401
        assert (await get(ident, "/v1/me", headers=ident.cookie(token))).status_code == 200
        assert await ident.audit("logout") == []


# --- 指标 ---


async def test_login_results_are_counted(identity_world: Any) -> None:
    """xuanpu_logins_total：成功与七种失败原因一开始都是 0，之后按结果计数；不带租户标签。"""
    async with identity_world() as ident:
        results = (
            "succeeded",
            "feishu_denied",
            "not_in_app_scope",
            "state_invalid",
            "tenant_mismatch",
            "tenant_unavailable",
            "user_disabled",
            "identity_unavailable",
        )
        assert logins(ident) == dict.fromkeys(results, 0.0)
        await begin(ident, tenant="acme")
        await ident.configure_feishu()
        await login(ident, ZHANG)
        await login(ident, ZHANG)
        counted = logins(ident)
        assert (counted["succeeded"], counted["tenant_unavailable"]) == (2.0, 1.0)
        assert counted["state_invalid"] == 0.0
