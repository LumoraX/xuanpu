"""租户的飞书配置：``GET /v1/identity/feishu``、``PUT /v1/identity/feishu``。

权限（只给租户管理员）、引用的密钥必须已在保管库、启用飞书登录必须配 ``public_url``、响应与审计
都不含密钥明文、要填进飞书后台的回调地址与事件地址。夹具 ``identity_world`` 见本目录的
``conftest.py``。
"""

from __future__ import annotations

from typing import Any

import pytest

BODY = {
    "app_id": "cli_test_app",
    "app_secret_ref": "feishu-app-secret",
    "encrypt_key_ref": "feishu-encrypt-key",
    "verification_token_ref": "feishu-verification-token",
}
PLAINTEXTS = {
    "feishu-app-secret": "app-secret-PLAINTEXT-1",
    "feishu-encrypt-key": "encrypt-key-PLAINTEXT-2",
    "feishu-verification-token": "verification-PLAINTEXT-3",
}
CALLBACK = "https://xuanpu.example.com/v1/auth/feishu/callback"
EVENTS = "https://xuanpu.example.com/v1/identity/feishu/events/acme"
WRITE = {"X-Requested-By": "xuanpu"}


def bearer(key: str) -> dict[str, str]:
    """带 API key 的请求头。"""
    return {"Authorization": f"Bearer {key}"}


async def write_secrets(ident: Any, names: tuple[str, ...] = tuple(PLAINTEXTS)) -> None:
    """以租户管理员身份把飞书应用的密钥写进保管库（经 HTTP）。"""
    for name in names:
        response = await ident.http.put(
            f"/v1/secrets/{name}",
            json={"value": PLAINTEXTS[name]},
            headers=bearer(ident.world.admin_key),
        )
        assert response.status_code == 200


def assert_no_plaintext(text: str) -> None:
    """文本里没有任何一个密钥的明文。"""
    for plaintext in PLAINTEXTS.values():
        assert plaintext not in text


async def test_put_then_get_without_plaintext(identity_world: Any) -> None:
    """写入后取回：只有密钥的名字，没有明文；附上回调地址与事件地址；记 identity_config_updated
    （只记 App ID、有效期与密钥名）。"""
    async with identity_world() as ident:
        await write_secrets(ident)
        admin = bearer(ident.world.admin_key)
        body = {**BODY, "session_ttl_seconds": 3600}
        put = await ident.http.put("/v1/identity/feishu", json=body, headers=admin)
        assert put.status_code == 200, put.text
        got = await ident.http.get("/v1/identity/feishu", headers=admin)
        assert got.status_code == 200
        assert (
            got.json()
            == put.json()
            == {
                "configured": True,
                **body,
                "feishu_tenant_key": None,
                "callback_url": CALLBACK,
                "event_url": EVENTS,
                "hint": None,
            }
        )
        assert_no_plaintext(put.text + got.text)
        (record,) = await ident.audit("identity_config_updated")
        assert record.actor == f"key:{ident.world.admin.key_id}"
        assert record.data == {
            "app_id": "cli_test_app",
            "session_ttl_seconds": 3600,
            "refs": {
                "app": "feishu-app-secret",
                "encrypt": "feishu-encrypt-key",
                "verification": "feishu-verification-token",
            },
        }
        assert_no_plaintext(str(record.data))


async def test_get_before_configuring(identity_world: Any) -> None:
    """还没配置：configured 为 false、各项为 null，回调地址与事件地址照样给出（先填进飞书后台）。"""
    async with identity_world() as ident:
        got = await ident.http.get("/v1/identity/feishu", headers=bearer(ident.world.admin_key))
        assert got.status_code == 200
        assert got.json() == {
            "configured": False,
            "app_id": None,
            "app_secret_ref": None,
            "encrypt_key_ref": None,
            "verification_token_ref": None,
            "session_ttl_seconds": None,
            "feishu_tenant_key": None,
            "callback_url": CALLBACK,
            "event_url": EVENTS,
            "hint": None,
        }


async def test_put_replaces_the_whole_config_but_keeps_the_binding(identity_world: Any) -> None:
    """PUT 是整体替换：没给的 session_ttl_seconds 恢复为平台默认（null）；已绑定的飞书企业保留。"""
    async with identity_world() as ident:
        await write_secrets(ident)
        admin = bearer(ident.world.admin_key)
        body = {**BODY, "session_ttl_seconds": 3600}
        await ident.http.put("/v1/identity/feishu", json=body, headers=admin)
        await ident.store.bind_tenant_key("acme", "feishu", "tk_acme")
        again = await ident.http.put(
            "/v1/identity/feishu", json={**BODY, "app_id": "cli_new"}, headers=admin
        )
        assert again.status_code == 200
        view = again.json()
        assert (view["app_id"], view["session_ttl_seconds"]) == ("cli_new", None)
        assert view["feishu_tenant_key"] == "tk_acme"
        assert len(await ident.audit("identity_config_updated")) == 2


async def test_only_tenant_admins(identity_world: Any) -> None:
    """成员（API key 或浏览器登录）、平台管理员都是 403；没有凭据 401；浏览器登录的管理员可以。"""
    async with identity_world() as ident:
        await write_secrets(ident)
        member = bearer(ident.world.member_key)
        root = bearer(ident.world.root_key)
        member_cookie = {**ident.cookie(await ident.login(await ident.user())), **WRITE}
        for headers in (member, root, member_cookie):
            got = await ident.http.get("/v1/identity/feishu", headers=headers)
            put = await ident.http.put("/v1/identity/feishu", json=BODY, headers=headers)
            assert (got.status_code, put.status_code) == (403, 403)
        assert (await ident.http.get("/v1/identity/feishu")).status_code == 401
        admin_user = await ident.user(role="tenant_admin", name="管理员")
        admin_cookie = {**ident.cookie(await ident.login(admin_user)), **WRITE}
        put = await ident.http.put("/v1/identity/feishu", json=BODY, headers=admin_cookie)
        assert put.status_code == 200
        (record,) = await ident.audit("identity_config_updated")
        assert record.user_id == admin_user.user_id
        assert await ident.store.get_identity_config("acme", "feishu") is not None


@pytest.mark.parametrize("missing", list(PLAINTEXTS))
async def test_referenced_secrets_must_exist(identity_world: Any, missing: str) -> None:
    """引用的三个密钥任一不在保管库：400 secret_missing，提示里有它的名字；什么都不写。"""
    async with identity_world() as ident:
        await write_secrets(ident, tuple(name for name in PLAINTEXTS if name != missing))
        put = await ident.http.put(
            "/v1/identity/feishu", json=BODY, headers=bearer(ident.world.admin_key)
        )
        assert put.status_code == 400
        error = put.json()["error"]
        assert error["code"] == "secret_missing"
        assert missing in error["message"]
        assert await ident.store.get_identity_config("acme", "feishu") is None
        assert await ident.audit("identity_config_updated") == []


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "app_secret_ref": "Bad_Name"},
        {**BODY, "session_ttl_seconds": 10},
        {**BODY, "extra": 1},
        {key: value for key, value in BODY.items() if key != "app_id"},
        {**BODY, "app_id": ""},
    ],
    ids=["bad_secret_name", "ttl_too_short", "extra_field", "missing_app_id", "empty_app_id"],
)
async def test_body_is_validated(identity_world: Any, body: dict[str, Any]) -> None:
    """请求体不合法：400 invalid_request。"""
    async with identity_world() as ident:
        await write_secrets(ident)
        put = await ident.http.put(
            "/v1/identity/feishu", json=body, headers=bearer(ident.world.admin_key)
        )
        assert put.status_code == 400
        assert put.json()["error"]["code"] == "invalid_request"


async def test_feishu_login_needs_public_url(identity_world: Any) -> None:
    """没配 public_url：PUT 是 400（启用飞书登录必须有对外地址）；GET 的两个地址为 null，附提示。"""
    async with identity_world(public_url=None) as ident:
        await write_secrets(ident)
        admin = bearer(ident.world.admin_key)
        put = await ident.http.put("/v1/identity/feishu", json=BODY, headers=admin)
        assert put.status_code == 400
        assert put.json()["error"]["code"] == "invalid_request"
        assert "public_url" in put.json()["error"]["message"]
        got = (await ident.http.get("/v1/identity/feishu", headers=admin)).json()
        assert (got["callback_url"], got["event_url"]) == (None, None)
        assert "public_url" in got["hint"]
        assert got["configured"] is False
