"""SDK 经真实身份 API 验证代调用、会话隔离与审计用量。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from xuanpu_sdk import XuanpuClient, XuanpuError
from xuanpu_server.api import create_app


async def test_sdk_user_identity_contract(identity_world: Any, step: Any) -> None:
    """管理员签发代调用 key；两个成员只看各自会话，用量与审计保留主人。"""
    async with identity_world() as ident:
        alice, bob = await ident.user(name="甲"), await ident.user(name="乙")
        transport = httpx.ASGITransport(app=create_app(ident.world.platform))
        async with XuanpuClient("http://x", ident.world.root_key, transport=transport) as root:
            key = await root.admin.issue_tenant_key("acme", "系统", role="tenant_admin", act_as_users=True)
        async with XuanpuClient("http://x", key.key, transport=transport) as admin:
            await admin.packages.publish(ident.world.archive(), activate=True)
            ordinary = await admin.admin.issue_key("普通", act_as_users=False)
            system_session = await admin.sessions.create("support-desk")
            async with XuanpuClient("http://x", key.key, act_as_user=alice.user_id, transport=transport) as a:
                me = await a.me()
                assert me["role"] == "tenant_member"
                assert me["user_id"] == alice.user_id
                session = await a.sessions.create("support-desk")
                assert session.user_id == alice.user_id
                ident.world.models.script(step(text="你好"))
                assert (await a.sessions.ask(session.session_id, "问候"))[0] == "你好"
                assert [row.session_id for row in await a.sessions.list()] == [session.session_id]
            async with XuanpuClient("http://x", key.key, act_as_user=bob.user_id, transport=transport) as b:
                with pytest.raises(XuanpuError) as denied:
                    await b.sessions.get(session.session_id)
                assert denied.value.status == 404
            async with XuanpuClient("http://x", ordinary.key, act_as_user=alice.user_id, transport=transport) as unprivileged:
                with pytest.raises(XuanpuError) as denied_key:
                    await unprivileged.me()
                assert denied_key.value.status == 403
            rows = [row async for row in admin.admin.audit(user_id=alice.user_id, actor=me["actor"], limit=1)]
            assert rows and all(row.user_id == alice.user_id and row.actor == me["actor"] for row in rows)
            usage = await admin.admin.user_usage("2000-01-01", "2999-12-31", user_id=alice.user_id)
            assert len(usage) == 1 and usage[0].user_id == alice.user_id and usage[0].turns == 1
            assert {row.session_id for row in await admin.sessions.list()} == {session.session_id, system_session.session_id}
