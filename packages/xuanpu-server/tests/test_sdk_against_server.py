"""用 SDK 走一遍真实的平台应用：SDK 与接口的契约。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from xuanpu_sdk import XuanpuClient, XuanpuError
from xuanpu_server.api import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def root_client(world: Any) -> AsyncIterator[XuanpuClient]:
    """拿根凭据的客户端，直接接在 ASGI 应用上。"""
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with XuanpuClient("http://xuanpu", world.root_key, transport=transport) as client:
        yield client


async def test_full_flow_with_sdk(world: Any, step: Any, root_client: XuanpuClient) -> None:
    """开通租户 → 签发凭据 → 配密钥 → 发布 → 会话 → 审批 → 审计与用量。"""
    await root_client.admin.create_tenant("globex", "Globex", max_active_sessions=5)
    issued = await root_client.admin.issue_tenant_key("globex", "admin", role="tenant_admin")
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with XuanpuClient("http://xuanpu", issued.key, transport=transport) as client:
        await client.secrets.put("crm-token", "globex-token")
        assert await client.secrets.names() == ["crm-token"]
        published = await client.packages.publish(world.archive(), activate=True)
        assert (published.name, published.version, published.active) == (
            "support-desk", "1.0.0", True,
        )  # fmt: skip
        assert [item.version for item in await client.packages.list("support-desk")] == ["1.0.0"]

        session = await client.sessions.create("support-desk")
        world.models.script(
            step(text="要更新", calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]),
            step(text="已更新"),
        )
        reply, last = await client.sessions.ask(session.session_id, "更新工单")
        assert (reply, last.kind) == ("要更新", "turn_suspended")
        request_id = last.data["pending"][0]["request_id"]
        resumed = [
            event
            async for event in client.sessions.resume(
                session.session_id, {request_id: {"granted": True}}
            )
        ]
        assert resumed[-1].kind == "turn_completed"
        assert "".join(event.text_delta for event in resumed) == "已更新"
        assert world.calls() == [{"name": "update_ticket", "arguments": {"id": "T-1"}}]

        kinds = [
            record.kind
            async for record in client.admin.audit(session_id=session.session_id, limit=3)
        ]
        assert {"turn_suspended", "connector_call", "turn_completed"} <= set(kinds)
        usage = await client.admin.usage("2000-01-01", "2999-12-31")
        assert [item.turns for item in usage] == [2]

        closed = await client.sessions.close(session.session_id)
        assert closed.status == "closed"
        with pytest.raises(XuanpuError) as caught:
            await client.sessions.ask(session.session_id, "还在吗")
        assert caught.value.code == "conflict"
        assert [item.session_id for item in await client.sessions.list(status="closed")] == [
            session.session_id
        ]
