"""飞书登录对未认证请求的加固：匿名请求刷不出审计、形状不对的 state 与授权码不查库也不调飞书、
每个租户同时在调飞书的登录有上限、再次登录让浏览器带着的旧登录会话作废。

夹具与浏览器动作见本目录的 ``conftest.py``（``identity_world``）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from xuanpu_server.identity.limits import FEISHU_CALLS_PER_TENANT

ZHANG = {"union_id": "on_zhang", "open_id": "ou_zhang", "name": "张三"}
LANDING = "/console/"


def person(index: int) -> dict[str, str]:
    """第 ``index`` 名员工。"""
    return {"union_id": f"on_{index}", "open_id": f"ou_{index}", "name": f"员工{index}"}


# --- 匿名请求刷不出审计 ---


async def test_anonymous_requests_write_no_audit(identity_world: Any) -> None:
    """没有真实飞书登录的请求，不论刷多少次都不产生审计记录，只计指标：对没配飞书的租户反复开始
    登录、伪造 error= 的回调、带假授权码的回调。"""
    async with identity_world() as ident:
        for _ in range(20):
            await ident.begin(tenant="acme")
        await ident.configure_feishu()
        for error in ("access_denied", "server_error", "<script>" + "x" * 500):
            state, _, binding = await ident.authorize(ZHANG)
            await ident.callback(binding=binding, state=state, error=error)
        for _ in range(5):
            state, _, binding = await ident.authorize(ZHANG)
            response = await ident.callback(binding=binding, state=state, code="bogus-code")
            assert ident.login_error(response) == "identity_unavailable"
        assert await ident.audit("login_failed") == []
        assert await ident.audit("user_created") == []
        counted = ident.logins()
        assert counted["tenant_unavailable"] == 20
        assert counted["feishu_denied"] == 1
        assert counted["identity_unavailable"] == 2 + 5


# --- 形状不对的 state 与授权码 ---


@pytest.mark.parametrize(
    "state",
    ["s" * 42, "s" * 44, "s" * 30_000, "s" * 42 + "!", "s" * 42 + "=", "ｓ" * 43],
    ids=["short", "long", "huge", "punctuation", "padding", "fullwidth"],
)
async def test_malformed_state_is_rejected_without_a_lookup(
    identity_world: Any, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """state 不是 43 个 base64url 字符：直接 state_invalid，不查登录尝试。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        looked_up: list[str] = []
        original = ident.store.take_login_attempt

        async def spy(value: str, *, now: float) -> Any:
            """记下查过哪些 state。"""
            looked_up.append(value)
            return await original(value, now=now)

        monkeypatch.setattr(ident.store, "take_login_attempt", spy)
        response = await ident.callback(binding="x", state=state, code="c")
        await ident.assert_login_failed(response, "state_invalid")
        assert looked_up == []


@pytest.mark.parametrize(
    "code",
    ["c" * 513, "c" * 30_000, "授权码", "has space", "tab\there", "nul\x00"],
    ids=["long", "huge", "non_ascii", "space", "tab", "nul"],
)
async def test_malformed_code_is_not_sent_to_feishu(identity_world: Any, code: str) -> None:
    """授权码超过 512 个字符或不全是可见 ASCII：identity_unavailable，不调飞书；登录尝试已作废。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        state, real_code, binding = await ident.authorize(ZHANG)
        response = await ident.callback(binding=binding, state=state, code=code)
        await ident.assert_login_failed(response, "identity_unavailable")
        assert ident.feishu.requests == []
        retry = await ident.callback(binding=binding, state=state, code=real_code)
        assert ident.login_error(retry) == "state_invalid"


async def test_code_of_512_visible_characters_is_sent(identity_world: Any) -> None:
    """512 个可见 ASCII 字符的授权码照常拿去换令牌（飞书说它无效，按 identity_unavailable）。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        state, _, binding = await ident.authorize(ZHANG)
        response = await ident.callback(binding=binding, state=state, code="~" * 512)
        assert ident.login_error(response) == "identity_unavailable"
        assert [request.url.path for request in ident.feishu.requests] == [
            "/open-apis/authen/v2/oauth/token"
        ]


# --- 每个租户同时在调飞书的登录有上限 ---


async def test_feishu_calls_per_tenant_are_capped(identity_world: Any) -> None:
    """同一租户同时在调飞书的登录到了上限（8 个），再来的排队；排队超时按 identity_unavailable，
    不调飞书。前面的放行之后照常完成。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        assert FEISHU_CALLS_PER_TENANT == 8
        login = ident.world.platform.identity.login
        login.feishu_limit.queue_seconds = 0.05
        flows = [await ident.authorize(person(index)) for index in range(9)]
        ident.feishu.hold = asyncio.Event()
        held = [
            asyncio.create_task(ident.callback(binding=b, state=s, code=c)) for s, c, b in flows[:8]
        ]
        async with asyncio.timeout(5):
            while ident.feishu.in_flight < 8:
                await asyncio.sleep(0.01)
        state, code, binding = flows[8]
        queued = await ident.callback(binding=binding, state=state, code=code)
        await ident.assert_login_failed(queued, "identity_unavailable")
        assert ident.feishu.in_flight == 8
        ident.feishu.hold.set()
        done = await asyncio.gather(*held)
        assert [response.headers["location"] for response in done] == [LANDING] * 8


async def test_queued_login_proceeds_when_a_slot_frees(identity_world: Any) -> None:
    """排队的登录在时限内等到名额，照常完成。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        ident.world.platform.identity.login.feishu_limit.queue_seconds = 5.0
        flows = [await ident.authorize(person(index)) for index in range(9)]
        ident.feishu.hold = asyncio.Event()
        tasks = [
            asyncio.create_task(ident.callback(binding=b, state=s, code=c)) for s, c, b in flows
        ]
        async with asyncio.timeout(5):
            while ident.feishu.in_flight < 8:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert ident.feishu.in_flight == 8  # 第 9 个在排队，没有发出去
        ident.feishu.hold.set()
        done = await asyncio.gather(*tasks)
        assert [response.headers["location"] for response in done] == [LANDING] * 9


# --- 再次登录让旧登录会话作废 ---


async def test_relogin_revokes_the_session_the_browser_carried(identity_world: Any) -> None:
    """浏览器带着（恰好一个）有效的登录 cookie 再次登录成功：那个旧登录会话作废（旧令牌 401），
    新的可用；别处的登录会话不受影响。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        old = ident.session_token(await ident.feishu_login(ZHANG))
        elsewhere = ident.session_token(await ident.feishu_login(ZHANG))
        state, code, binding = await ident.authorize(ZHANG)
        cookies = f"{ident.cookie_name}={old}"
        done = await ident.callback(binding=binding, cookies=cookies, state=state, code=code)
        new = ident.session_token(done)
        assert (await ident.get("/v1/me", headers=ident.cookie(old))).status_code == 401
        assert (await ident.get("/v1/me", headers=ident.cookie(new))).status_code == 200
        assert (await ident.get("/v1/me", headers=ident.cookie(elsewhere))).status_code == 200


@pytest.mark.parametrize("case", ["two_cookies", "login_failed"])
async def test_old_session_survives_when_not_clearly_replaced(
    identity_world: Any, case: str
) -> None:
    """同名的登录 cookie 不止一个（不知道哪个是这个浏览器的），或这次登录失败了：旧登录会话不动。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        old = ident.session_token(await ident.feishu_login(ZHANG))
        other = ident.session_token(await ident.feishu_login(ZHANG))
        state, code, binding = await ident.authorize(ZHANG)
        cookies = f"{ident.cookie_name}={old}"
        if case == "two_cookies":
            cookies += f"; {ident.cookie_name}={other}"
        else:
            ident.feishu.fail["token"] = httpx.Response(503, text="busy")
        await ident.callback(binding=binding, cookies=cookies, state=state, code=code)
        for token in (old, other):
            assert (await ident.get("/v1/me", headers=ident.cookie(token))).status_code == 200
