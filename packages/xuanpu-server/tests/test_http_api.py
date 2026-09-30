"""HTTP 接入层：经真实的 ASGI 应用走一遍完整流程。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from xuanpu_server.api import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def bearer(key: str) -> dict[str, str]:
    """带凭据的请求头。"""
    return {"Authorization": f"Bearer {key}"}


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 响应体拆成 ``(事件名, 数据)``。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        name, data = "", {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name:
            frames.append((name, data))
    return frames


@pytest.fixture
async def client(world: Any) -> AsyncIterator[httpx.AsyncClient]:
    """连到应用上的 HTTP 客户端。"""
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as http:
        yield http


async def test_health_needs_no_credentials(client: httpx.AsyncClient) -> None:
    """存活探测不需要凭据。"""
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer nope"}, {"Authorization": ""}],
)
async def test_missing_or_bad_credentials(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    """没有凭据或凭据无效：统一的 401。"""
    response = await client.get("/v1/sessions", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthenticated", "message": "凭据无效"}}


async def test_full_flow_over_http(world: Any, step: Any, client: httpx.AsyncClient) -> None:
    """建租户 → 签发凭据 → 配密钥 → 发布业务包 → 建会话 → 对话 → 审批 → 查审计与用量。"""
    root = bearer(world.root_key)
    created = await client.post(
        "/v1/admin/tenants",
        headers=root,
        json={"tenant_id": "globex", "name": "Globex", "quota": {"max_active_sessions": 3}},
    )
    assert created.status_code == 201
    assert created.json()["quota"] == {"max_active_sessions": 3, "max_daily_tokens": None}

    issued = await client.post(
        "/v1/admin/tenants/globex/keys",
        headers=root,
        json={"name": "admin", "role": "tenant_admin"},
    )
    assert issued.status_code == 201
    admin = bearer(issued.json()["key"])
    assert "secret_hash" not in issued.json()

    assert (
        await client.put("/v1/secrets/crm-token", headers=admin, json={"value": "globex-token"})
    ).status_code == 200
    assert (await client.get("/v1/secrets", headers=admin)).json() == {"names": ["crm-token"]}

    published = await client.post(
        "/v1/packages?activate=true",
        headers={**admin, "Content-Type": "application/zip"},
        content=world.archive(),
    )
    assert published.status_code == 201, published.text
    assert published.json()["active"] is True
    assert "location" not in published.json()

    session = await client.post("/v1/sessions", headers=admin, json={"package": "support-desk"})
    assert session.status_code == 201
    session_id = session.json()["session_id"]

    world.models.script(
        step(text="我来更新", calls=[("c1", "mcp__crm__update_ticket", {"id": "T-9"})]),
        step(text="已更新"),
    )
    first = await client.post(
        f"/v1/sessions/{session_id}/messages", headers=admin, json={"text": "更新工单 T-9"}
    )
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(first.text)
    assert frames[0][0] == "turn"
    assert frames[0][1]["submission_id"] == first.headers["x-submission-id"]
    assert [name for name, _ in frames][-2:] == ["turn_suspended", "end"]
    assert ("assistant_text", "我来更新") in [
        (name, data["data"].get("delta")) for name, data in frames if name == "assistant_text"
    ]
    request_id = frames[-2][1]["data"]["pending"][0]["request_id"]
    assert world.calls() == []

    second = await client.post(
        f"/v1/sessions/{session_id}/resume",
        headers=admin,
        json={"resolutions": {request_id: {"granted": True}}},
    )
    assert [name for name, _ in parse_sse(second.text)][-2:] == ["turn_completed", "end"]
    assert world.calls() == [{"name": "update_ticket", "arguments": {"id": "T-9"}}]

    audit = await client.get(
        "/v1/audit", headers=admin, params={"session_id": session_id, "limit": 1000}
    )
    recorded = [record["kind"] for record in audit.json()["records"]]
    assert recorded[0] == "session_created"
    assert {"turn_suspended", "suspension_resolved_by", "connector_call", "turn_completed"} <= set(
        recorded
    )
    assert "globex-token" not in audit.text

    gate = await client.get("/v1/audit", headers=admin, params={"source": "gate"})
    assert [record["data"]["tool"] for record in gate.json()["records"]] == ["update_ticket"]

    usage = await client.get(
        "/v1/usage", headers=admin, params={"since": "2000-01-01", "until": "2999-12-31"}
    )
    assert [item["turns"] for item in usage.json()["usage"]] == [2]

    closed = await client.delete(f"/v1/sessions/{session_id}", headers=admin)
    assert closed.json()["status"] == "closed"
    rejected = await client.post(
        f"/v1/sessions/{session_id}/messages", headers=admin, json={"text": "还在吗"}
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "conflict"


async def test_audit_pagination(world: Any, step: Any, client: httpx.AsyncClient) -> None:
    """审计按 ``next_after_id`` 翻页，不重不漏。"""
    await world.publish()
    admin = bearer(world.admin_key)
    seen: list[int] = []
    after: int | None = 0
    while after is not None:
        page = await client.get("/v1/audit", headers=admin, params={"after_id": after, "limit": 2})
        seen.extend(record["audit_id"] for record in page.json()["records"])
        after = page.json()["next_after_id"]
    total = await client.get("/v1/audit", headers=admin, params={"limit": 1000})
    assert seen == [record["audit_id"] for record in total.json()["records"]]
    assert len(seen) >= 4


async def test_role_and_tenant_errors_over_http(world: Any, client: httpx.AsyncClient) -> None:
    """权限不足是 403；别的租户的资源是 404；根凭据不能直接进租户。"""
    await world.publish()
    member, admin, root = (
        bearer(world.member_key), bearer(world.admin_key), bearer(world.root_key),
    )  # fmt: skip
    session = await client.post("/v1/sessions", headers=member, json={"package": "support-desk"})
    session_id = session.json()["session_id"]

    for method, path, kwargs in (
        ("PUT", "/v1/secrets/x", {"json": {"value": "v"}}),
        ("GET", "/v1/secrets", {}),
        ("GET", "/v1/audit", {}),
        ("GET", "/v1/keys", {}),
        ("POST", "/v1/packages", {"content": b"zip"}),
        ("POST", "/v1/admin/tenants", {"json": {"tenant_id": "evil-corp", "name": "x"}}),
        ("GET", "/v1/admin/tenants", {}),
    ):
        response = await client.request(method, path, headers=member, **kwargs)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["error"]["code"] == "permission_denied"

    assert (await client.get("/v1/admin/tenants", headers=admin)).status_code == 403
    assert (await client.get("/v1/sessions", headers=root)).status_code == 403

    await client.post(
        "/v1/admin/tenants", headers=root, json={"tenant_id": "globex", "name": "Globex"}
    )
    other = await client.post(
        "/v1/admin/tenants/globex/keys", headers=root, json={"name": "a", "role": "tenant_admin"}
    )
    outsider = bearer(other.json()["key"])
    for method, path, kwargs in (
        ("GET", f"/v1/sessions/{session_id}", {}),
        ("DELETE", f"/v1/sessions/{session_id}", {}),
        ("POST", f"/v1/sessions/{session_id}/messages", {"json": {"text": "窃听"}}),
        ("POST", f"/v1/sessions/{session_id}/resume", {"json": {"resolutions": {"r": {}}}}),
        ("POST", "/v1/sessions", {"json": {"package": "support-desk"}}),
        ("POST", "/v1/packages/support-desk/activate", {"json": {"version": "1.0.0"}}),
    ):
        response = await client.request(method, path, headers=outsider, **kwargs)
        assert response.status_code == 404, (method, path, response.text)
    assert (await client.get("/v1/sessions", headers=outsider)).json() == {"sessions": []}
    assert (await client.get("/v1/packages", headers=outsider)).json() == {"packages": []}
    assert (await client.get("/v1/audit", headers=outsider)).json()["records"][0]["kind"] == (
        "tenant_created"
    )


async def test_key_management_over_http(world: Any, client: httpx.AsyncClient) -> None:
    """租户管理员签发、列出、吊销凭据；吊销后立即失效。"""
    admin = bearer(world.admin_key)
    issued = await client.post("/v1/keys", headers=admin, json={"name": "ci"})
    assert issued.status_code == 201
    assert issued.json()["role"] == "tenant_member"
    key, key_id = issued.json()["key"], issued.json()["key_id"]
    assert (await client.get("/v1/sessions", headers=bearer(key))).status_code == 200

    listed = await client.get("/v1/keys", headers=admin)
    assert key_id in [item["key_id"] for item in listed.json()["keys"]]
    assert key not in listed.text

    assert (await client.delete(f"/v1/keys/{key_id}", headers=admin)).json() == {"revoked": True}
    assert (await client.get("/v1/sessions", headers=bearer(key))).status_code == 401
    assert (await client.delete(f"/v1/keys/{key_id}", headers=admin)).status_code == 404

    escalate = await client.post(
        "/v1/keys", headers=admin, json={"name": "x", "role": "platform_admin"}
    )
    assert escalate.status_code == 400


async def test_invalid_requests(world: Any, client: httpx.AsyncClient) -> None:
    """不合法的请求是 400，并指出问题所在；多余的字段不被接受。"""
    await world.publish()
    admin = bearer(world.admin_key)
    cases = [
        ("POST", "/v1/sessions", {"json": {}}),
        ("POST", "/v1/sessions", {"json": {"package": "support-desk", "unexpected": 1}}),
        ("POST", "/v1/sessions", {"json": {"package": "support-desk", "entry": "faq"}}),
        ("POST", "/v1/sessions", {"content": b"not json"}),
        ("GET", "/v1/sessions?limit=0", {}),
        ("GET", "/v1/sessions?status=weird", {}),
        ("GET", "/v1/usage?since=yesterday&until=today", {}),
        ("GET", "/v1/audit?source=nowhere", {}),
        ("PUT", "/v1/secrets/Bad_Name", {"json": {"value": "v"}}),
        ("PUT", "/v1/secrets/ok-name", {"json": {"value": ""}}),
    ]
    for method, path, kwargs in cases:
        response = await client.request(method, path, headers=admin, **kwargs)
        assert response.status_code == 400, (method, path, response.text)
        assert response.json()["error"]["code"] == "invalid_request"

    bad = await client.post("/v1/packages", headers=admin, content=b"not a zip")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "package_invalid"

    session = await client.post("/v1/sessions", headers=admin, json={"package": "support-desk"})
    empty = await client.post(
        f"/v1/sessions/{session.json()['session_id']}/messages", headers=admin, json={"text": " "}
    )
    assert empty.status_code == 400


async def test_quota_and_conflict_status_codes(world: Any, client: httpx.AsyncClient) -> None:
    """超出配额是 429；重复发布是 409。"""
    root, admin = bearer(world.root_key), bearer(world.admin_key)
    await client.post(
        "/v1/admin/tenants",
        headers=root,
        json={"tenant_id": "tiny-co", "name": "Tiny", "quota": {"max_active_sessions": 1}},
    )
    issued = await client.post(
        "/v1/admin/tenants/tiny-co/keys", headers=root, json={"name": "a", "role": "tenant_admin"}
    )
    tiny = bearer(issued.json()["key"])
    await client.put("/v1/secrets/crm-token", headers=tiny, json={"value": "t"})
    await client.post("/v1/packages?activate=true", headers=tiny, content=world.archive())
    assert (
        await client.post("/v1/sessions", headers=tiny, json={"package": "support-desk"})
    ).status_code == 201
    over = await client.post("/v1/sessions", headers=tiny, json={"package": "support-desk"})
    assert over.status_code == 429
    assert over.json()["error"]["code"] == "quota_exceeded"

    await client.post("/v1/packages", headers=admin, content=world.archive())
    again = await client.post("/v1/packages", headers=admin, content=world.archive())
    assert again.status_code == 409

    suspended = await client.put(
        "/v1/admin/tenants/tiny-co/status", headers=root, json={"status": "suspended"}
    )
    assert suspended.json()["status"] == "suspended"
    assert (await client.get("/v1/sessions", headers=tiny)).status_code == 401


async def test_unexpected_error_does_not_leak_details(
    world: Any, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未预料的异常：调用方只看到笼统的 500，细节不外泄。"""

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(world.platform.store, "list_sessions", boom)
    transport = httpx.ASGITransport(app=create_app(world.platform), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as http:
        response = await http.get("/v1/sessions", headers=bearer(world.admin_key))
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "服务内部错误"}}


async def test_console_is_served_when_configured(world: Any, tmp_path: Any) -> None:
    """给了静态目录就挂在 /console/，不给就没有。"""
    console = tmp_path / "console"
    console.mkdir()
    (console / "index.html").write_text("<title>xuanpu 控制台</title>")
    (console / "app.js").write_text("console.log(1)")
    with_console = httpx.ASGITransport(app=create_app(world.platform, console_dir=console))
    async with httpx.AsyncClient(transport=with_console, base_url="http://xuanpu") as http:
        page = await http.get("/console/")
        assert page.status_code == 200
        assert "xuanpu 控制台" in page.text
        assert (await http.get("/console/app.js")).status_code == 200
        assert (await http.get("/console/../xuanpu.yaml")).status_code in (400, 404)
        # 控制台不需要凭据，但接口仍然需要
        assert (await http.get("/v1/sessions")).status_code == 401
    without = httpx.ASGITransport(app=create_app(world.platform))
    async with httpx.AsyncClient(transport=without, base_url="http://xuanpu") as http:
        assert (await http.get("/console/")).status_code == 404


async def test_user_messages_are_audited(world: Any, step: Any, client: httpx.AsyncClient) -> None:
    """用户发的消息记进审计（超长截断），审批送回也有记录。"""
    await world.publish()
    admin = bearer(world.admin_key)
    session = await client.post("/v1/sessions", headers=admin, json={"package": "support-desk"})
    session_id = session.json()["session_id"]
    world.models.script(step(text="好"))
    await client.post(f"/v1/sessions/{session_id}/messages", headers=admin, json={"text": "问题一"})
    audit = await client.get(
        "/v1/audit", headers=admin, params={"session_id": session_id, "kind": "message_sent"}
    )
    records = audit.json()["records"]
    assert [record["data"]["text"] for record in records] == ["问题一"]
    assert records[0]["data"]["submission_id"].startswith("sub")
