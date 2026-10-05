"""工作台 HTTP 权限、严格请求和安全下载。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.identity import LoginSession, UserRecord
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.cookies import login_cookie_name, new_login_token
from xuanpu_server.identity.service import IdentitySettings


@pytest.fixture
async def api(world_opener, local_sandboxes):
    """真实浏览器认证与两名员工身份。"""
    settings = IdentitySettings.from_config("https://xuanpu.example.com", IdentityConfig())
    world = await world_opener(
        store=InMemoryControlPlane(), sandboxes=local_sandboxes(), identity_settings=settings
    )
    await world.platform.reminders.close()
    users = {}
    for user_id in ("usr_a", "usr_b"):
        await world.platform.identity.store.create_user(
            UserRecord(
                tenant_id="acme", user_id=user_id, display_name=user_id, created_at=time.time()
            )
        )
        token, hashed = new_login_token()
        now = time.time()
        await world.platform.identity.store.create_login_session(
            LoginSession(
                token_hash=hashed,
                tenant_id="acme",
                user_id=user_id,
                created_at=now,
                expires_at=now + 3600,
                last_seen_at=now,
            )
        )
        users[user_id] = {"Cookie": f"{login_cookie_name(settings.cookie_secure)}={token}"}
    app = create_app(world.platform)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://xuanpu.example.com"
    ) as client:
        try:
            yield SimpleNamespace(world=world, client=client, users=users, app=app)
        finally:
            await world.platform.close()


def headers(api, user="usr_a"):
    """实际同源 CSRF 标识。"""
    return {**api.users[user], "X-Requested-By": "xuanpu", "Origin": "https://xuanpu.example.com"}


async def test_http_crud_owner_and_machine_isolation(api):
    """真实路由拒绝机器身份、CSRF和员工越权。"""
    client = api.client
    body = {"kind": "todo", "data": {"title": "任务"}, "request_id": "one"}
    assert (
        await client.post("/v1/workspace/items", json=body, headers=api.users["usr_a"])
    ).status_code == 403
    created = await client.post("/v1/workspace/items", json=body, headers=headers(api))
    assert created.status_code == 200, created.text
    item = created.json()
    assert (await client.get("/v1/workspace/items", headers=api.users["usr_b"])).json() == {
        "items": []
    }
    assert (
        await client.get(f"/v1/workspace/items/{item['id']}", headers=api.users["usr_b"])
    ).status_code == 404
    assert (
        await client.get(
            "/v1/workspace/settings", headers={"Authorization": f"Bearer {api.world.admin_key}"}
        )
    ).status_code == 403
    assert (
        await client.post(
            "/v1/workspace/items", json={**body, "user_id": "usr_b"}, headers=headers(api)
        )
    ).status_code == 400
    assert (
        await client.delete(f"/v1/workspace/items/{item['id']}?version=1", headers=headers(api))
    ).json()["trashed_at"] is not None
    assert (
        await client.post(
            f"/v1/workspace/items/{item['id']}/restore?version=2", headers=headers(api)
        )
    ).json()["version"] == 3


async def test_http_upload_versions_and_safe_download(api):
    """上传正本、版本读回和浏览器强制附件响应。"""
    created = await api.client.post(
        "/v1/workspace/files",
        params={"name": "报告.html", "request_id": "upload"},
        content=b"<script>alert(1)</script>",
        headers={**headers(api), "Content-Type": "text/html"},
    )
    assert created.status_code == 200, created.text
    item = created.json()
    downloaded = await api.client.get(
        f"/v1/workspace/files/{item['id']}/content", headers=api.users["usr_a"]
    )
    assert downloaded.status_code == 200 and downloaded.content == b"<script>alert(1)</script>"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert (
        await api.client.get(
            f"/v1/workspace/files/{item['id']}/content", headers=api.users["usr_b"]
        )
    ).status_code == 404
    assert (
        await api.client.get(
            f"/v1/workspace/files/{item['id']}/versions", headers=api.users["usr_a"]
        )
    ).json()["items"][0]["revision"] == 1
    assert (
        await api.client.post(
            "/v1/workspace/messages",
            json={"role": "assistant", "text": "injection"},
            headers=headers(api),
        )
    ).status_code in (404, 405)
    invalid = await api.client.post(
        "/v1/workspace/items",
        json={
            "kind": "event",
            "data": {"title": "x", "start_at": 5, "end_at": 4},
            "request_id": "bad",
        },
        headers=headers(api),
    )
    assert invalid.status_code == 400


async def test_history_before_cursor_http_bounds(api):
    """上界游标拒绝零值和数据库整数溢出。"""
    for value in ("0", "9223372036854775808", "nonsense"):
        response = await api.client.get(
            "/v1/workspace/messages", params={"before_id": value}, headers=api.users["usr_a"]
        )
        assert response.status_code == 400
