"""飞书登录与加密事件独立配置：只配 App Secret 可登录，事件缺项时关闭。"""

from __future__ import annotations

from typing import Any

import pytest
from xuanpu_server.identity.cookies import login_attempt_cookie_name

EVENTS = "/v1/identity/feishu/events/acme"
BASE = {"app_id": "cli_test_app", "app_secret_ref": "feishu-app-secret"}


@pytest.mark.parametrize(
    "optional",
    [
        {},
        {"encrypt_key_ref": None, "verification_token_ref": None},
        {"encrypt_key_ref": "", "verification_token_ref": ""},
        {"verification_token_ref": "feishu-verification-token"},
        {"encrypt_key_ref": "feishu-encrypt-key"},
    ],
)
async def test_event_secrets_optional_but_callbacks_closed(
    identity_world: Any, optional: dict[str, str | None]
) -> None:
    """缺任一事件密钥引用均允许保存；明文及伪造加密事件都明确返回 404。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        admin = {"Authorization": "Bearer " + ident.world.admin_key}
        response = await ident.http.put("/v1/identity/feishu", headers=admin, json=BASE | optional)
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["configured"] is True
        assert view["events_enabled"] is False
        for name in ("encrypt_key_ref", "verification_token_ref"):
            assert view[name] == (optional.get(name) or None)
        got = await ident.http.get("/v1/identity/feishu", headers=admin)
        assert got.json() == view
        for body in ({"type": "url_verification", "challenge": "plain"}, {"encrypt": "fake"}):
            for headers in ({}, {"X-Lark-Signature": "fake", "X-Lark-Request-Timestamp": "1", "X-Lark-Request-Nonce": "n"}):
                rejected = await ident.http.post(EVENTS, json=body, headers=headers)
                assert rejected.status_code == 404, rejected.text
                assert rejected.json()["error"]["code"] == "not_found"
                assert "事件配置" in rejected.json()["error"]["message"]


async def test_login_works_without_event_secrets(identity_world: Any) -> None:
    """移除事件密钥后仍完成真实登录流程的授权码交换，不伪造登录 cookie。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        admin = {"Authorization": "Bearer " + ident.world.admin_key}
        for name in ("feishu-encrypt-key", "feishu-verification-token"):
            deleted = await ident.http.delete("/v1/secrets/" + name, headers=admin)
            assert deleted.status_code == 200
        configured = await ident.http.put("/v1/identity/feishu", headers=admin, json=BASE)
        assert configured.status_code == 200, configured.text
        started = await ident.begin(tenant="acme")
        person = ident.feishu.person(union_id="optional-user", open_id="open-user", name="员工")
        state, code = ident.feishu.authorize(started.headers["location"], person)
        binding = ident.cookie_value(started, login_attempt_cookie_name(ident.secure))
        done = await ident.callback(binding=binding, state=state, code=code)
        assert done.headers["location"] == "/console/"
        me = await ident.get("/v1/me", headers=ident.cookie(ident.session_token(done)))
        assert me.status_code == 200
        assert me.json()["display_name"] == "员工"


@pytest.mark.parametrize("field", ["encrypt_key_ref", "verification_token_ref"])
async def test_provided_optional_secret_must_exist(identity_world: Any, field: str) -> None:
    """可选项只要提供，就仍须存在，不能用不存在的引用伪装启用。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        response = await ident.http.put(
            "/v1/identity/feishu", json=BASE | {field: "missing-event-secret"},
            headers={"Authorization": "Bearer " + ident.world.admin_key},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "secret_missing"


@pytest.mark.parametrize("app_secret", [None, ""])
async def test_app_secret_stays_required(identity_world: Any, app_secret: str | None) -> None:
    """事件可以关闭，登录所需的 App Secret 不允许为空。"""
    async with identity_world() as ident:
        body = {"app_id": "cli_test_app"}
        if app_secret is not None:
            body["app_secret_ref"] = app_secret
        response = await ident.http.put(
            "/v1/identity/feishu", json=body,
            headers={"Authorization": "Bearer " + ident.world.admin_key},
        )
        assert response.status_code == 400


async def test_existing_event_config_stays_enabled(identity_world: Any) -> None:
    """已配置双密钥的租户保持事件开启；两项重新省略时关闭。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        admin = {"Authorization": "Bearer " + ident.world.admin_key}
        response = await ident.http.get("/v1/identity/feishu", headers=admin)
        assert response.json()["events_enabled"] is True
        replaced = await ident.http.put("/v1/identity/feishu", json=BASE, headers=admin)
        assert replaced.json()["events_enabled"] is False


async def test_unconfigured_events_are_disabled(identity_world: Any) -> None:
    """未配置租户的响应显式说明事件关闭。"""
    async with identity_world() as ident:
        response = await ident.http.get(
            "/v1/identity/feishu", headers={"Authorization": "Bearer " + ident.world.admin_key}
        )
        assert response.json()["events_enabled"] is False
