"""SDK 身份契约：旧响应兼容、筛选与固定代调用身份。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from xuanpu_sdk import AuditRecord, Session, XuanpuClient


@pytest.mark.parametrize("user_id", [None, "usr_example"])
def test_identity_models_accept_old_and_new_responses(user_id: str | None) -> None:
    """旧服务端没有 user_id，新服务端保留会话及审计主人。"""
    identity = {} if user_id is None else {"user_id": user_id}
    session = Session.from_json({
        "session_id": "s", "package": "p", "version": "1", "entry": "e",
        "status": "active", "owner": None, "created_at": 1, "updated_at": 1,
        "created_by": "key:k", **identity,
    })
    audit = AuditRecord.from_json({
        "audit_id": 1, "source": "control", "kind": "session_created", "at": 1,
        "actor": "key:k", "session_id": "s", "stream": None, "seq": None,
        "data": {}, **identity,
    })
    assert session.user_id == audit.user_id == user_id


async def test_audit_filters_survive_pagination() -> None:
    """按员工和触发者查询时每一页都保留条件。"""
    pages: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """校验查询参数并模拟两页。"""
        assert request.url.params["user_id"] == "usr_owner"
        assert request.url.params["actor"] == "key:k/user:usr_actor"
        after = request.url.params["after_id"]
        pages.append(after)
        return httpx.Response(200, json={"records": [], "next_after_id": 1 if after == "0" else None})

    async with XuanpuClient("http://x", "k", transport=httpx.MockTransport(handle)) as client:
        assert [row async for row in client.admin.audit(user_id="usr_owner", actor="key:k/user:usr_actor")] == []
    assert pages == ["0", "1"]


async def test_user_usage_and_me() -> None:
    """暴露当前身份和按用户用量，后者可限定员工。"""
    def handle(request: httpx.Request) -> httpx.Response:
        """响应与平台路由一致。"""
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"actor": "key:k", "user_id": None})
        assert request.url.path == "/v1/usage/users"
        assert dict(request.url.params) == {"since": "2026-10-01", "until": "2026-10-03", "user_id": "usr_u"}
        return httpx.Response(200, json={"usage": [{
            "day": "2026-10-02", "user_id": "usr_u", "input_tokens": 3,
            "output_tokens": 2, "total_tokens": 5, "turns": 1,
        }]})

    async with XuanpuClient("http://x", "k", transport=httpx.MockTransport(handle)) as client:
        assert (await client.me())["actor"] == "key:k"
        rows = await client.admin.user_usage("2026-10-01", "2026-10-03", user_id="usr_u")
        assert (rows[0].user_id, rows[0].total_tokens) == ("usr_u", 5)


@pytest.mark.parametrize("platform", [False, True])
async def test_issue_act_as_key(platform: bool) -> None:
    """两种签发入口显式传递代调用能力，默认仍关闭。"""
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """记录签发请求。"""
        assert request.url.path == ("/v1/admin/tenants/acme/keys" if platform else "/v1/keys")
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"key": "new", "key_id": "id", "tenant_id": "acme", "name": "job", "role": "tenant_member"})

    async with XuanpuClient("http://x", "k", transport=httpx.MockTransport(handle)) as client:
        if platform:
            await client.admin.issue_tenant_key("acme", "job", act_as_users=True)
            await client.admin.issue_tenant_key("acme", "job")
        else:
            await client.admin.issue_key("job", act_as_users=True)
            await client.admin.issue_key("job")
    assert bodies[0]["act_as_users"] is True
    assert bodies[1].get("act_as_users", False) is False


async def test_act_as_header_on_requests_and_streams() -> None:
    """一个客户端固定代表一人，普通及流式请求均带身份；另一个客户端不受影响。"""
    headers: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """同时接收普通和 SSE 请求。"""
        assert request.headers["Authorization"] == "Bearer k"
        headers.append(request.headers.get("X-Xuanpu-Act-As"))
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, text='event: turn_completed\ndata: {"kind":"turn_completed","submission_id":"sub","seq":1,"at":1,"data":{"is_root":true}}\n\n')
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handle)
    async with XuanpuClient("http://x", "k", act_as_user="usr_u", transport=transport) as client:
        await client.health()
        assert len([event async for event in client.sessions.send("s", "你好")]) == 1
    async with XuanpuClient("http://x", "k", transport=transport) as plain:
        await plain.health()
    assert headers == ["usr_u", "usr_u", None]
