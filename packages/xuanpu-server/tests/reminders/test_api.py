"""提醒HTTP身份隔离、CSRF和SSE持久补读验证。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from xuanpu_server.api import create_app
from xuanpu_server.api.reminders import notification_events
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.identity import LoginSession, UserRecord
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.cookies import login_cookie_name, new_login_token
from xuanpu_server.identity.service import IdentitySettings
from xuanpu_server.reminders.models import ReminderInput
from xuanpu_server.tenancy import Principal


@pytest.fixture
async def api(world_opener, local_sandboxes):
    """实际认证路由配两名员工登录cookie，关闭定时扫描以精确控制到期。"""
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


def payload(**changes):
    """HTTP创建请求。"""
    return dict(
        dict(
            title="提交报告",
            due_at=time.time() + 100,
            timezone="Asia/Shanghai",
            request_id="request-1",
        ),
        **changes,
    )


def write_headers(api, user="usr_a"):
    """同源浏览器写请求携带既有CSRF标识。"""
    return {**api.users[user], "X-Requested-By": "xuanpu", "Origin": "https://xuanpu.example.com"}


async def test_employee_api_crud_isolation_csrf(api):
    """真实cookie认证、CSRF和员工范围守卫都作用于提醒路由。"""
    client = api.client
    assert (
        await client.post("/v1/reminders", json=payload(), headers=api.users["usr_a"])
    ).status_code == 403
    created = await client.post("/v1/reminders", json=payload(), headers=write_headers(api))
    assert created.status_code == 200, created.text
    record = created.json()
    assert "original_fingerprint" not in record
    assert (await client.get("/v1/reminders", headers=api.users["usr_b"])).json() == {"items": []}
    denied = await client.delete(
        f"/v1/reminders/{record['reminder_id']}?version=1", headers=write_headers(api, "usr_b")
    )
    assert denied.status_code == 404
    update = {k: v for k, v in payload(title="修改后").items() if k != "request_id"}
    update["version"] = 1
    changed = await client.put(
        f"/v1/reminders/{record['reminder_id']}", json=update, headers=write_headers(api)
    )
    assert changed.status_code == 200 and changed.json()["version"] == 2
    assert (
        await client.delete(
            f"/v1/reminders/{record['reminder_id']}?version=1", headers=write_headers(api)
        )
    ).status_code == 409
    assert (
        await client.delete(
            f"/v1/reminders/{record['reminder_id']}?version=2", headers=write_headers(api)
        )
    ).json()["status"] == "cancelled"


async def test_api_rejects_system_key_injection_and_reports_channels(api):
    """不带员工的key和额外身份字段均不能创建，渠道据已配能力返回。"""
    response = await api.client.get(
        "/v1/reminders", headers={"Authorization": f"Bearer {api.world.admin_key}"}
    )
    assert response.status_code == 403
    response = await api.client.post(
        "/v1/reminders", json=payload(user_id="usr_b"), headers=write_headers(api)
    )
    assert response.status_code == 400
    response = await api.client.get("/v1/reminders/channels", headers=api.users["usr_a"])
    assert response.json() == {"channels": ["inbox"]}


async def test_notification_read_and_invalid_sse_cursor(api):
    """通知只允许本人标读；SSE初始身份和游标先校验。"""
    service = api.world.platform.reminders
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    record = await service.create(
        who, ReminderInput(title="提醒", due_at=time.time() + 100, timezone="UTC"), "notification"
    )
    notification = await service.store.fire(record, time.time() + 101)
    response = await api.client.get("/v1/notifications?latest=true", headers=api.users["usr_a"])
    assert response.json()["items"][0]["notification_id"] == notification.notification_id
    assert response.json()["items"][0]["deliveries"] == []
    assert (
        await api.client.post(
            f"/v1/notifications/{notification.notification_id}/read",
            headers=write_headers(api, "usr_b"),
        )
    ).status_code == 404
    assert (
        await api.client.post(
            f"/v1/notifications/{notification.notification_id}/read", headers=write_headers(api)
        )
    ).json()["read_at"] is not None
    response = await api.client.get(
        "/v1/notifications/events", headers={**api.users["usr_a"], "Last-Event-ID": "nonsense"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("closure", ["logout", "disabled"])
async def test_sse_replay_and_logout_closes_existing_stream(api, closure):
    """真实caller重认证使既存流在退出后结束，重连按持久游标补读。"""
    platform = api.world.platform
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    records = []
    for i in range(2):
        record = await platform.reminders.create(
            who,
            ReminderInput(title=f"提醒{i}", due_at=time.time() + 100, timezone="UTC"),
            f"sse-{i}",
        )
        records.append(await platform.reminders.store.fire(record, time.time() + 101))
    raw_cookie = api.users["usr_a"]["Cookie"]

    async def receive():
        """模拟尚未断开的HTTP接收通道。"""
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/notifications/events",
            "headers": [(b"cookie", raw_cookie.encode())],
            "app": api.app,
        },
        receive=receive,
    )
    stream = notification_events(request, platform.reminders, who, records[0].notification_id)
    event = await anext(stream)
    assert f"id: {records[1].notification_id}\n" in event
    assert "event: notification\n" in event
    assert await anext(stream) == ": heartbeat\n\n"
    token = raw_cookie.split("=", 1)[1]
    if closure == "logout":
        await platform.identity.logout(who, token)
    else:
        await platform.identity.store.set_user_status(
            "acme", "usr_a", "disabled", reason="admin", at=time.time()
        )
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 3)
    await stream.aclose()


async def test_notification_cursor_cannot_overflow_database(api):
    """客户端超大游标应是请求错误，不交给数据库整数转换。"""
    response = await api.client.get(
        "/v1/notifications?after_id=9223372036854775808", headers=api.users["usr_a"]
    )
    assert response.status_code == 400
