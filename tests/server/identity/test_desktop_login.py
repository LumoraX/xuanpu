"""桌面客户端的浏览器接力登录（ADR 0021）：等待页、浏览器回调与确认页、确认、轮询、换取。

两个「浏览器」分开模拟：WebView 带登录尝试 cookie；系统浏览器不带它。cookie 由用例显式带上
（见本目录 ``conftest.py``）。
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from server.domains.identity.cookies import login_attempt_cookie_name, login_token_hash
from server.domains.identity.handoff import confirm_code

if TYPE_CHECKING:
    import httpx

ZHANG = {"union_id": "on_zhang", "open_id": "ou_zhang", "name": "张三"}
LANDING = "/console/"
STATUS = "/v1/auth/feishu/desktop/status"
REDEEM = "/v1/auth/feishu/desktop/redeem"
CONFIRM = "/v1/auth/feishu/desktop/confirm"


def _match(pattern: str, text: str) -> str:
    """页面里某个值（反转义后）。"""
    found = re.search(pattern, text)
    assert found is not None, pattern
    return html.unescape(found.group(1))


async def _start(ident: Any, **params: str) -> tuple[httpx.Response, str]:
    """WebView 开始桌面登录，返回 ``(等待页, 登录尝试 cookie)``。"""
    page = await ident.begin(tenant="acme", client="desktop", **params)
    assert page.status_code == 200, page.text
    return page, ident.cookie_value(page, login_attempt_cookie_name(ident.secure))


async def _browser_callback(ident: Any, page: httpx.Response) -> httpx.Response:
    """员工在系统浏览器里授权，飞书回调落在浏览器（不带登录尝试 cookie）。"""
    authorize = _match(r'href="([^"]+)" target="_blank"', page.text)
    state, code = ident.feishu.authorize(authorize, ident.feishu.person(**ZHANG))
    return await ident.callback(binding=None, state=state, code=code)


async def _status(ident: Any, cookie: str | None) -> str:
    """WebView 轮询。"""
    name = login_attempt_cookie_name(ident.secure)
    headers = {"Cookie": f"{name}={cookie}"} if cookie else {}
    response = await ident.get(STATUS, headers=headers)
    assert response.headers["cache-control"] == "no-store"
    status: str = response.json()["status"]
    return status


async def _redeem(ident: Any, cookie: str | None) -> httpx.Response:
    """WebView 换取登录会话。"""
    name = login_attempt_cookie_name(ident.secure)
    headers = {"Cookie": f"{name}={cookie}"} if cookie else {}
    return await ident.get(REDEEM, headers=headers)


async def _confirm(ident: Any, body: str) -> httpx.Response:
    """员工在浏览器确认页点「确认登录」。"""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response: httpx.Response = await ident.http.post(CONFIRM, content=body, headers=headers)
    ident.http.cookies.clear()
    return response


async def test_desktop_login_hands_off_through_the_browser(identity_world: Any) -> None:
    """完整接力：等待页显示确认码并写 cookie；浏览器回调显示同一确认码、不写登录 cookie；
    员工确认前换取失败但记录还在；确认后 WebView 换到登录会话，跳回 return_to；一切只能一次。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        page, cookie = await _start(ident, return_to="/console/sessions")
        code = confirm_code(login_token_hash(cookie))
        assert f'<div class="code">{code}</div>' in page.text
        assert "script-src 'nonce-" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"
        assert await _status(ident, cookie) == "pending"

        shown = await _browser_callback(ident, page)
        assert shown.status_code == 200
        assert f'<div class="code">{code}</div>' in shown.text
        assert ident.cookie_name not in ident.set_cookies(shown)
        assert await _status(ident, cookie) == "confirm"
        await ident.assert_login_failed(await _redeem(ident, cookie), "state_invalid")
        assert await _status(ident, cookie) == "confirm"

        nonce = _match(r'name="nonce" value="([^"]+)"', shown.text)
        confirmed = await _confirm(ident, f"nonce={nonce}")
        assert confirmed.status_code == 200 and "已确认" in confirmed.text
        assert (await _confirm(ident, f"nonce={nonce}")).status_code == 400
        assert await _status(ident, cookie) == "ready"

        done = await _redeem(ident, cookie)
        assert done.status_code == 302
        assert done.headers["location"] == "/console/sessions"
        assert ident.attempt_cleared(done)
        me = await ident.get("/v1/me", headers=ident.cookie(ident.session_token(done)))
        assert me.json()["display_name"] == "张三"
        (record,) = await ident.audit("login_succeeded")
        assert record.data == {"provider": "feishu", "client": "desktop"}
        assert await _status(ident, cookie) == "pending"
        assert (await _redeem(ident, cookie)).headers["location"].endswith("state_invalid")


async def test_redeem_needs_the_webview_cookie(identity_world: Any) -> None:
    """确认过的接力只能由带着那个登录尝试 cookie 的 WebView 换取；没带 cookie 的轮询是 invalid。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        page, cookie = await _start(ident)
        shown = await _browser_callback(ident, page)
        nonce = _match(r'name="nonce" value="([^"]+)"', shown.text)
        assert (await _confirm(ident, f"nonce={nonce}")).status_code == 200
        assert await _status(ident, None) == "invalid"
        await ident.assert_login_failed(await _redeem(ident, None), "state_invalid")
        await ident.assert_login_failed(await _redeem(ident, "x" * 43), "state_invalid")
        assert (await _redeem(ident, cookie)).headers["location"] == LANDING


async def test_confirm_rejects_bad_forms(identity_world: Any) -> None:
    """确认表单：缺字段、字段重复、未知确认串、超长都按确认失败（400），不改动接力记录。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        page, cookie = await _start(ident)
        shown = await _browser_callback(ident, page)
        nonce = _match(r'name="nonce" value="([^"]+)"', shown.text)
        for body in ("", f"nonce={nonce}&nonce={nonce}", "nonce=unknown", "nonce=" + "a" * 2000):
            assert (await _confirm(ident, body)).status_code == 400
        assert await _status(ident, cookie) == "confirm"


async def test_redeem_refuses_a_user_disabled_meanwhile(identity_world: Any) -> None:
    """浏览器确认之后、换取之前员工被停用：换取失败（user_disabled），不签发登录会话。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        page, cookie = await _start(ident)
        shown = await _browser_callback(ident, page)
        nonce = _match(r'name="nonce" value="([^"]+)"', shown.text)
        assert (await _confirm(ident, f"nonce={nonce}")).status_code == 200
        user = await ident.store.get_user_by_identity("acme", "feishu", "on_zhang")
        await ident.store.set_user_status("acme", user.user_id, "disabled", reason="admin", at=1.0)
        await ident.assert_login_failed(await _redeem(ident, cookie), "user_disabled", audited=True)


async def test_browser_login_unchanged_and_unknown_client_rejected(identity_world: Any) -> None:
    """不带 client 仍是 302 去飞书；client 只收 browser 与 desktop。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        assert (await ident.begin(tenant="acme")).status_code == 302
        assert (await ident.begin(tenant="acme", client="browser")).status_code == 302
        assert (await ident.begin(tenant="acme", client="mobile")).status_code == 400


async def test_desktop_begin_failure_redirects_to_landing(identity_world: Any) -> None:
    """桌面登录开始就失败（租户没配飞书）：与浏览器一样 302 回落地页，不显示等待页。"""
    async with identity_world() as ident:
        failed = await ident.begin(tenant="acme", client="desktop")
        await ident.assert_login_failed(failed, "tenant_unavailable")


async def test_desktop_user_agent_starts_the_handoff_without_client(identity_world: Any) -> None:
    """桌面客户端的用户代理带 XuanpuDesktop 标记：没带 client 也返回等待页；显式 client=browser
    仍按浏览器；普通浏览器的用户代理照旧 302 去飞书。"""
    desktop = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 XuanpuDesktop/2026.11.18"}
    safari = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"}
    async with identity_world() as ident:
        await ident.configure_feishu()
        page = await ident.get("/v1/auth/feishu/login", params={"tenant": "acme"}, headers=desktop)
        assert page.status_code == 200 and '<div class="code">' in page.text
        forced = {"tenant": "acme", "client": "browser"}
        assert (await ident.get("/v1/auth/feishu/login", params=forced, headers=desktop)).status_code == 302
        plain = await ident.get("/v1/auth/feishu/login", params={"tenant": "acme"}, headers=safari)
        assert plain.status_code == 302
