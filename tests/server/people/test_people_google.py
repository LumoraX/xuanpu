"""纯个人空间 Google 账号关联与 Google 通讯录只读同步基础代码测试（纯本地 mock，不连外网）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from server.api import create_app
from server.core.controlplane import AuditQuery, InMemoryControlPlane
from server.core.tenancy import Principal
from server.domains.identity import LoginSession, UserRecord
from server.domains.identity.config import IdentityConfig
from server.domains.identity.cookies import login_cookie_name, new_login_token
from server.domains.identity.service import IdentitySettings
from server.domains.people.google_client import (
    GoogleConnectionsPage,
    GoogleSyncTokenExpiredError,
    GoogleTokenBundle,
    GoogleUserInfo,
    google_person_to_profile,
)
from server.domains.people.google_config import GoogleContactsConfig


class FakeGoogleClient:
    """可编程本地测试桩：模拟 Google OAuth2 与 People API，绝不访问外网。"""

    def __init__(self) -> None:
        self.exchanged: list[dict[str, str]] = []
        self.refreshed: list[str] = []
        self.revoked: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        self.pages: list[GoogleConnectionsPage] = []
        self.expire_sync_token_once = False
        self.userinfo = GoogleUserInfo(
            sub="google-sub-001", email="alice@gmail.com", display_name="Alice Gmail"
        )
        self.next_access_token = "ya29.initial-secret-token"  # noqa: S105
        self.next_refresh_token: str | None = "1//initial-refresh-secret"  # noqa: S105
        self.expires_in = 3600

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleTokenBundle:
        self.exchanged.append({
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        })
        return GoogleTokenBundle(
            access_token=self.next_access_token,
            expires_in=self.expires_in,
            refresh_token=self.next_refresh_token,
            scope="openid email profile https://www.googleapis.com/auth/contacts.readonly",
        )

    async def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenBundle:
        self.refreshed.append(refresh_token)
        return GoogleTokenBundle(
            access_token="ya29.refreshed-token",  # noqa: S106
            expires_in=3600,
            refresh_token=refresh_token,
            scope="openid email profile https://www.googleapis.com/auth/contacts.readonly",
        )

    async def fetch_userinfo(
        self, *, access_token: str, id_token: str = ""
    ) -> GoogleUserInfo:
        return self.userinfo

    async def list_connections(
        self,
        *,
        access_token: str,
        page_token: str | None = None,
        sync_token: str | None = None,
        page_size: int = 200,
    ) -> GoogleConnectionsPage:
        self.list_calls.append({
            "access_token": access_token,
            "page_token": page_token,
            "sync_token": sync_token,
        })
        if self.expire_sync_token_once and sync_token is not None:
            self.expire_sync_token_once = False
            raise GoogleSyncTokenExpiredError("expired")
        if self.pages:
            return self.pages.pop(0)
        return GoogleConnectionsPage(connections=[], next_sync_token="sync_empty")  # noqa: S106

    async def revoke_token(self, *, token: str) -> None:
        self.revoked.append(token)

    async def close(self) -> None:
        return


async def _open_env(
    world_opener,
    local_sandboxes,
    *,
    google_cfg: GoogleContactsConfig | None = None,
    google_client: FakeGoogleClient | None = None,
    clock: list[float] | None = None,
):
    now_box = clock or [1_700_000_000.0]
    settings = IdentitySettings.from_config("https://xuanpu.example.com", IdentityConfig())
    store = InMemoryControlPlane()
    world = await world_opener(
        store=store,
        sandboxes=local_sandboxes(),
        identity_settings=settings,
        google_contacts=google_cfg,
        google_contacts_client=google_client,
        now=lambda: now_box[0],
    )
    await world.platform.reminders.close()
    users: dict[str, dict[str, str]] = {}
    principals: dict[str, Principal] = {}
    for user_id in ("usr_a", "usr_b"):
        await world.platform.identity.store.create_user(
            UserRecord(
                tenant_id="acme",
                user_id=user_id,
                display_name=user_id,
                created_at=now_box[0],
            )
        )
        token, hashed = new_login_token()
        await world.platform.identity.store.create_login_session(
            LoginSession(
                token_hash=hashed,
                tenant_id="acme",
                user_id=user_id,
                created_at=now_box[0],
                expires_at=now_box[0] + 7200,
                last_seen_at=now_box[0],
            )
        )
        users[user_id] = {"Cookie": f"{login_cookie_name(settings.cookie_secure)}={token}"}
        principals[user_id] = Principal(
            key_id=None, role="member", tenant_id="acme", user_id=user_id
        )
    app = create_app(world.platform)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://xuanpu.example.com",
        follow_redirects=False,
    )
    return SimpleNamespace(
        world=world,
        store=store,
        client=client,
        users=users,
        principals=principals,
        clock=now_box,
        google=google_client,
    )


def hdr(env, user: str = "usr_a") -> dict[str, str]:
    """附带同源 CSRF 头的员工浏览器请求头。"""
    return {**env.users[user], "X-Requested-By": "xuanpu", "Origin": "https://xuanpu.example.com"}


def test_google_person_mapper_fields_and_deleted() -> None:
    """Google People JSON 正确映射为 PersonProfile，且支持 metadata.deleted。"""
    mapped = google_person_to_profile({
        "resourceName": "people/c1001",
        "etag": "%Etag1",
        "names": [{"displayName": "林致远", "metadata": {"primary": True}}],
        "nicknames": [{"value": "林总"}],
        "organizations": [{"name": "星河科技", "department": "研发部", "title": "CTO"}],
        "phoneNumbers": [
            {"value": "+86 138-0013-8000", "type": "mobile", "metadata": {"primary": True}}
        ],
        "emailAddresses": [{"value": "lin@galaxy.example", "type": "work"}],
        "imClients": [{"protocol": "WeChat", "username": "wx_lin"}],
        "birthdays": [{"date": {"year": 1988, "month": 10, "day": 3}}],
        "genders": [{"value": "male"}],
        "addresses": [{"city": "上海", "formattedValue": "上海市浦东新区张江路1号"}],
        "urls": [{"value": "https://galaxy.example"}],
        "biographies": [{"value": "Google 通讯录备注"}],
    })
    assert mapped.deleted is False
    assert mapped.resource_name == "people/c1001"
    assert mapped.profile is not None
    p = mapped.profile
    assert p["name"] == "林致远"
    assert p["aliases"] == ["林总"]
    assert p["organization"] == "星河科技"
    assert p["department"] == "研发部"
    assert p["title"] == "CTO"
    assert p["gender"] == "male"
    assert p["birth"] == {"year": 1988, "month": 10, "day": 3, "calendar": "solar"}
    assert p["city"] == "上海"
    assert p["extra"]["address"] == "上海市浦东新区张江路1号"
    assert p["extra"]["website"] == "https://galaxy.example"
    assert {c["kind"] for c in p["contacts"]} == {"phone", "email", "wechat"}

    deleted = google_person_to_profile({
        "resourceName": "people/c1002",
        "etag": "%Etag2",
        "metadata": {"deleted": True},
    })
    assert deleted.deleted is True
    assert deleted.profile is None


async def test_google_contacts_disabled_by_default_no_outbound_calls(
    world_opener, local_sandboxes
) -> None:
    """默认关闭配置下不发起任何外部请求，状态接口安全返回 configured=False。"""
    env = await _open_env(world_opener, local_sandboxes)
    try:
        # 机器 API key 拒绝访问
        machine = await env.client.get(
            "/v1/people/google/status",
            headers={"Authorization": "Bearer " + env.world.admin_key},
        )
        assert machine.status_code == 403

        st = await env.client.get("/v1/people/google/status", headers=hdr(env))
        assert st.status_code == 200
        body = st.json()
        assert body["enabled"] is False
        assert body["configured"] is False
        assert body["linked"] is False
        assert body["callback_url"] == "https://xuanpu.example.com/v1/people/google/callback"

        start = await env.client.post("/v1/people/google/connect/start", headers=hdr(env))
        assert start.status_code == 409
        pv = await env.client.post("/v1/people/google/preview", headers=hdr(env))
        assert pv.status_code == 409
    finally:
        await env.client.aclose()
        await env.world.platform.close()


async def test_google_oauth_pkce_encrypted_storage_sync_refresh_and_unlink(
    world_opener, local_sandboxes
) -> None:
    """完整闭环：PKCE 授权、密文令牌、查重与增量同步、令牌刷新、410 回退、隔离与解绑。"""
    fake = FakeGoogleClient()
    cfg = GoogleContactsConfig(
        enabled=True,
        client_id="google-client-123",
        client_secret="google-secret-456",  # noqa: S106
    )
    env = await _open_env(world_opener, local_sandboxes, google_cfg=cfg, google_client=fake)
    try:
        # 1. 发起授权：验证 PKCE S256、contacts.readonly scope 与一次性 Cookie
        start = await env.client.post(
            "/v1/people/google/connect/start",
            headers=hdr(env, "usr_a"),
            json={"return_to": "/"},
        )
        assert start.status_code == 200, start.text
        auth_url = start.json()["authorization_url"]
        qs = parse_qs(urlsplit(auth_url).query)
        assert qs["client_id"] == ["google-client-123"]
        assert qs["code_challenge_method"] == ["S256"]
        assert qs["access_type"] == ["offline"]
        assert "https://www.googleapis.com/auth/contacts.readonly" in qs["scope"][0]
        state = qs["state"][0]
        attempt_cookie = start.cookies.get("__Host-xuanpu_google")
        assert attempt_cookie

        # 缺少尝试 Cookie 拒绝回调
        bad_cb = await env.client.get(
            f"/v1/people/google/callback?state={state}&code=auth-code-1",
            headers=env.users["usr_a"],
        )
        assert bad_cb.status_code == 401

        # 重新发起一次授权（上次 state 已被一次性消费），演示正常回调
        start2 = await env.client.post(
            "/v1/people/google/connect/start",
            headers=hdr(env, "usr_a"),
            json={"return_to": "/"},
        )
        state2 = parse_qs(urlsplit(start2.json()["authorization_url"]).query)["state"][0]
        cookie2 = start2.cookies.get("__Host-xuanpu_google")
        assert cookie2
        both_cookies = f"{env.users['usr_a']['Cookie']}; __Host-xuanpu_google={cookie2}"

        # 跨员工回调应拒绝（usr_b 不能用 usr_a 的 state）
        cross_cb = await env.client.get(
            f"/v1/people/google/callback?state={state2}&code=auth-code-2",
            headers={"Cookie": f"{env.users['usr_b']['Cookie']}; __Host-xuanpu_google={cookie2}"},
        )
        assert cross_cb.status_code == 401

        # 再起一次，由 usr_a 本人完成回调
        start3 = await env.client.post(
            "/v1/people/google/connect/start",
            headers=hdr(env, "usr_a"),
            json={"return_to": "/"},
        )
        state3 = parse_qs(urlsplit(start3.json()["authorization_url"]).query)["state"][0]
        cookie3 = start3.cookies.get("__Host-xuanpu_google")
        both_cookies = f"{env.users['usr_a']['Cookie']}; __Host-xuanpu_google={cookie3}"

        ok_cb = await env.client.get(
            f"/v1/people/google/callback?state={state3}&code=auth-code-ok",
            headers={"Cookie": both_cookies},
        )
        assert ok_cb.status_code == 302
        assert ok_cb.headers["location"] == "/?google_contacts=linked"
        assert fake.exchanged[-1]["code"] == "auth-code-ok"

        # 确认底层存储的令牌是密文，不含明文 token
        stored = await env.store.google_contacts_store.get_account("acme", "usr_a")
        assert stored is not None
        assert b"ya29.initial-secret-token" not in stored.access_token_cipher
        assert stored.refresh_token_cipher is not None
        assert b"1//initial-refresh-secret" not in stored.refresh_token_cipher

        # 状态接口可见关联信息，usr_b 保持隔离
        st_a = (await env.client.get("/v1/people/google/status", headers=hdr(env, "usr_a"))).json()
        assert st_a["linked"] is True
        assert st_a["account_email"] == "alice@gmail.com"
        st_b = (await env.client.get("/v1/people/google/status", headers=hdr(env, "usr_b"))).json()
        assert st_b["linked"] is False

        # 2. 先本地创建一个「老王」，再从 Google 拉取 2 条（老王重复 + 陈琳新增）
        seed = await env.client.post(
            "/v1/people",
            headers=hdr(env, "usr_a"),
            json={
                "request_id": "seed-local-wang",
                "profile": {
                    "name": "老王",
                    "contacts": [
                        {"kind": "phone", "value": "13800138000", "label": "手机", "primary": True}
                    ],
                },
            },
        )
        wang_id = seed.json()["id"]

        fake.pages.append(
            GoogleConnectionsPage(
                connections=[
                    {
                        "resourceName": "people/c1",
                        "etag": "etag-1",
                        "names": [{"displayName": "王建国"}],
                        "organizations": [{"name": "星辰科技", "title": "VP"}],
                        "phoneNumbers": [{"value": "+86 138-0013-8000", "type": "mobile"}],
                        "emailAddresses": [{"value": "wang@star.example", "type": "work"}],
                    },
                    {
                        "resourceName": "people/c2",
                        "etag": "etag-2",
                        "names": [{"displayName": "陈琳"}],
                        "organizations": [{"name": "明镜资本"}],
                        "phoneNumbers": [{"value": "13900139000", "type": "mobile"}],
                    },
                ],
                next_sync_token="sync-tok-1",  # noqa: S106
            )
        )
        pv1 = await env.client.post("/v1/people/google/preview", headers=hdr(env, "usr_a"))
        assert pv1.status_code == 200, pv1.text
        pv1_body = pv1.json()
        assert pv1_body["new_count"] == 1
        assert pv1_body["duplicate_count"] == 1
        assert pv1_body["items"][0]["status"] == "duplicate"
        assert pv1_body["items"][0]["candidates"][0]["person_id"] == wang_id
        assert pv1_body["items"][1]["status"] == "new"

        # 确认预览批次提交
        commit1 = await env.client.post(
            "/v1/people/google/sync",
            headers=hdr(env, "usr_a"),
            json={
                "mode": "preview",
                "request_id": "gsync-1",
                "next_sync_token": pv1_body["next_sync_token"],
                "deleted_resource_names": [],
                "items": [
                    {
                        "resource_name": "people/c1",
                        "etag": "etag-1",
                        "action": "merge",
                        "target_person_id": wang_id,
                        "target_version": 1,
                        "profile": pv1_body["items"][0]["profile"],
                    },
                    {
                        "resource_name": "people/c2",
                        "etag": "etag-2",
                        "action": "create",
                        "profile": pv1_body["items"][1]["profile"],
                    },
                ],
            },
        )
        assert commit1.status_code == 201, commit1.text
        assert commit1.json()["created_count"] == 1
        assert commit1.json()["merged_count"] == 1
        assert commit1.json()["has_sync_token"] is True

        # 3. 推进时钟触发 access_token 自动刷新 + 增量同步识别已关联联系人 + 410 自动回退
        env.clock[0] += 4000.0
        fake.expire_sync_token_once = True
        fake.pages.append(
            GoogleConnectionsPage(
                connections=[
                    {
                        "resourceName": "people/c1",
                        "etag": "etag-1b",
                        "names": [{"displayName": "王建国"}],
                        "organizations": [{"name": "星辰科技", "title": "高级副总裁"}],
                        "phoneNumbers": [{"value": "+86 138-0013-8000", "type": "mobile"}],
                        "biographies": [{"value": "二次同步补充备注"}],
                    },
                    {
                        "resourceName": "people/c2",
                        "etag": "etag-del",
                        "metadata": {"deleted": True},
                    },
                ],
                next_sync_token="sync-tok-2",  # noqa: S106
            )
        )
        auto_sync = await env.client.post(
            "/v1/people/google/sync",
            headers=hdr(env, "usr_a"),
            json={"mode": "auto", "request_id": "gsync-2"},
        )
        assert auto_sync.status_code == 201, auto_sync.text
        assert len(fake.refreshed) == 1
        assert auto_sync.json()["merged_count"] == 1
        assert auto_sync.json()["created_count"] == 0
        assert auto_sync.json()["deleted_links_count"] == 1

        # 4. 解除关联：撤销令牌、删除映射，保留已导入本地联系人
        unlink = await env.client.post("/v1/people/google/unlink", headers=hdr(env, "usr_a"))
        assert unlink.status_code == 200
        assert unlink.json()["unlinked"] is True
        assert fake.revoked == ["1//initial-refresh-secret"]

        st_after_resp = await env.client.get("/v1/people/google/status", headers=hdr(env, "usr_a"))
        assert st_after_resp.json()["linked"] is False
        people_resp = await env.client.get("/v1/people", headers=hdr(env, "usr_a"))
        assert len(people_resp.json()["items"]) == 2

        # 5. 脱敏审计检查：含 linked / synced / unlinked，严禁泄露邮箱、令牌或联系人 PII
        audits = await env.store.query_audit("acme", AuditQuery())
        kinds = {ev.kind for ev in audits}
        assert {"people.google.linked", "people.google.synced", "people.google.unlinked"} <= kinds
        blob = str([ev.data for ev in audits])
        for secret in ("alice@gmail.com", "ya29.", "1//initial", "王建国", "陈琳", "13800138000"):
            assert secret not in blob
    finally:
        await env.client.aclose()
        await env.world.platform.close()
