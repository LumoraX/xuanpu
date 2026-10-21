"""管理入口、原始审计与个人正文的权限闭环。"""
from __future__ import annotations

from typing import Any

import pytest
from xuanpu_server.controlplane.models import AuditRecord


@pytest.mark.parametrize("credential", ["cookie", "api_key"])
async def test_admin_metadata_does_not_grant_personal_content(identity_world: Any, credential: str) -> None:
    """管理员能运维，但不能经历史、待办、回放或发送接口取得个人内容。"""
    async with identity_world() as ident:
        await ident.world.publish()
        alice = await ident.user(name="甲")
        admin = await ident.user(name="管理", role="tenant_admin")
        own = {**ident.cookie(await ident.login(alice)), "X-Requested-By": "xuanpu"}
        manager = ({**ident.cookie(await ident.login(admin)), "X-Requested-By": "xuanpu"}
                   if credential == "cookie" else
                   {"Authorization": "Bearer " + ident.world.admin_key})
        created = await ident.http.post("/v1/sessions", headers=own,
            json={"package": "support-desk", "metadata": {"private": "PRIVATE-7d83"}})
        assert created.status_code == 201, created.text
        session = created.json()["session_id"]
        await ident.world.platform.store.append_audit([
            AuditRecord("acme", "engine", "turn_text", 1, "engine", session_id=session,
                        user_id=alice.user_id, data={"text": "PRIVATE-7d83"})])
        for suffix in ("", "/history", "/pending", "/events?submission_id=old"):
            response = await ident.http.get(f"/v1/sessions/{session}{suffix}", headers=manager)
            assert "PRIVATE-7d83" not in response.text
            assert response.status_code == (200 if not suffix else 404)
        for suffix, body in [("/messages", {"text": "继续"}),
                             ("/resume", {"resolutions": {"req": {"granted": True}}})]:
            response = await ident.http.post(f"/v1/sessions/{session}{suffix}", headers=manager, json=body)
            assert response.status_code == 404
        routed = await ident.http.post("/v1/assistant/route", headers=manager,
            json={"text": "继续", "session_id": session})
        assert routed.status_code == 404
        listed = await ident.http.get("/v1/sessions", headers=manager)
        assert "PRIVATE-7d83" not in listed.text
        own_history = await ident.http.get(f"/v1/sessions/{session}/history", headers=own)
        assert own_history.status_code == 200
        assert "PRIVATE-7d83" in own_history.text
        raw = await ident.http.get("/v1/audit", headers=manager)
        assert raw.status_code == 200
        assert "PRIVATE-7d83" not in raw.text
        assert all("data" not in record for record in raw.json()["records"])
        closed = await ident.http.delete(f"/v1/sessions/{session}", headers=manager)
        assert closed.status_code == 200
        assert "PRIVATE-7d83" not in closed.text
