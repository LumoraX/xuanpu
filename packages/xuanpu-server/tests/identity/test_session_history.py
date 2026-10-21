"""员工历史只读本人会话，输出按事件和字段双重白名单裁剪。"""

from __future__ import annotations

from typing import Any

import pytest
from xuanpu_server.controlplane.models import AuditRecord
from xuanpu_server.platform import ROOT_PRINCIPAL


@pytest.fixture
async def history(identity_world: Any) -> Any:
    """建两位成员和第一位成员的会话，给历史注入含敏感字段的记录。"""
    async with identity_world() as ident:
        await ident.world.publish()
        alice = await ident.user(name="甲")
        bob = await ident.user(name="乙")
        headers = ident.cookie(await ident.login(alice))
        created = await ident.http.post(
            "/v1/sessions",
            headers={**headers, "X-Requested-By": "xuanpu"},
            json={"package": "support-desk"},
        )
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        records = []
        for kind, data in [
            ("model_request", {"api_key": "private-key"}),
            ("message_sent", {"text": "你好", "secret": "private-key"}),
            (
                "tool_call_completed",
                {
                    "name": "lookup",
                    "is_error": False,
                    "duration_ms": 2,
                    "output": "private-key",
                    "arguments": {"token": "private-key"},
                },
            ),
            ("turn_text", {"text": "答复", "token": "private-key"}),
            ("turn_failed", {"error": "private-key"}),
        ]:
            records.append(
                AuditRecord(
                    tenant_id="acme",
                    session_id=session_id,
                    source="engine",
                    kind=kind,
                    at=1,
                    actor="engine",
                    data=data,
                )
            )
        await ident.world.platform.store.append_audit(records)
        yield ident, headers, ident.cookie(await ident.login(bob)), session_id


async def test_history_owner_and_redaction(history: Any) -> None:
    """本人看到对话与安全工具摘要，但不获得凭据、工具输出或内部字段。"""
    ident, headers, _, session_id = history
    response = await ident.http.get(f"/v1/sessions/{session_id}/history", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["kind"] for r in body["records"]] == [
        "message_sent",
        "tool_call_completed",
        "turn_text",
        "turn_failed",
    ]
    assert body["records"][1]["data"] == {"name": "lookup", "is_error": False, "duration_ms": 2}
    assert all(set(r) == {"audit_id", "at", "kind", "data"} for r in body["records"])
    assert "private-key" not in response.text
    assert body["next_after_id"] is None
    audit = await ident.http.get("/v1/audit", headers=headers)
    assert audit.status_code == 403


async def test_history_visibility(history: Any) -> None:
    """其他成员及跨租户看不到会话，无凭据不能查询。"""
    ident, _, bob_headers, session_id = history
    path = f"/v1/sessions/{session_id}/history"
    assert (await ident.get(path, headers=bob_headers)).status_code == 404
    assert (await ident.get(path)).status_code == 401
    await ident.world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id="other", name="外部")
    outsider = await ident.user(name="外部", tenant="other")
    assert (
        await ident.get(path, headers=ident.cookie(await ident.login(outsider)))
    ).status_code == 404


async def test_history_pagination(history: Any) -> None:
    """游标推进原始页，包含被过滤事件的页也不会丢掉后续文本。"""
    ident, headers, _, session_id = history
    after, records = 0, []
    for _ in range(20):
        response = await ident.get(
            f"/v1/sessions/{session_id}/history",
            headers=headers,
            params={"after_id": after, "limit": 1},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["records"]) <= 1
        records.extend(body["records"])
        if body["next_after_id"] is None:
            break
        assert body["next_after_id"] > after
        after = body["next_after_id"]
    else:
        pytest.fail("历史游标未结束")
    assert [r["kind"] for r in records] == [
        "message_sent",
        "tool_call_completed",
        "turn_text",
        "turn_failed",
    ]
    for params in (
        {"after_id": -1},
        {"limit": 201},
        {"limit": 0},
        {"tenant": "other"},
        {"user_id": "other"},
        {"kind": "model_request"},
    ):
        response = await ident.get(
            f"/v1/sessions/{session_id}/history", headers=headers, params=params
        )
        assert response.status_code == 400


async def test_history_safe_lifecycle_and_admin(history: Any) -> None:
    """生命周期仅展示种类，本人可读生命周期，未代人的系统 key 不可读。"""
    ident, owner_headers, _, session_id = history
    platform = ident.world.platform
    for kind in ("turn_started", "turn_suspended", "turn_completed"):
        await platform.store.append_audit(
            [
                AuditRecord(
                    tenant_id="acme",
                    session_id=session_id,
                    source="engine",
                    kind=kind,
                    at=2,
                    actor="engine",
                    data={"provider_state": "private-key"},
                )
            ]
        )
    response = await ident.get(
        f"/v1/sessions/{session_id}/history",
        headers=owner_headers,
    )
    assert response.status_code == 200
    records = response.json()["records"]
    assert [r["kind"] for r in records[-3:]] == ["turn_started", "turn_suspended", "turn_completed"]
    assert all(r["data"] == {} for r in records[-3:])
    assert "private-key" not in response.text
    # 未代人的系统 key 不获得员工内容权限。
    response = await ident.get(
        f"/v1/sessions/{session_id}/history",
        headers={"Authorization": f"Bearer {ident.world.member_key}"},
    )
    assert response.status_code == 404
