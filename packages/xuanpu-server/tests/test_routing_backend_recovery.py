"""两个控制面后端上的路由崩溃恢复与稳定会话契约。"""

from __future__ import annotations

import json

import pytest
from xuanpu_server.assistant import AssistantRouter, RouteBody
from xuanpu_server.errors import RuntimeUnavailableError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.tenancy import Principal


async def test_route_restarts_after_session_commit_without_resampling(
    identity_stores, world_opener, local_sandboxes, step, monkeypatch
):
    """新的路由器实例从持久选择恢复同一会话，覆盖真实PostgreSQL。"""
    control, identity = identity_stores
    world = await world_opener(store=control, identity_store=identity, sandboxes=local_sandboxes())
    try:
        await world.publish()
        await identity.create_user(
            UserRecord(
                tenant_id="acme", user_id="usr_route", display_name="路由员工", created_at=1.0
            )
        )
        who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_route", key_id=None)
        original = world.platform.workspace.complete_route
        failed = False

        async def fail_once(*args, **kwargs):
            """会话已经提交，路由结果还未保存。"""
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("commit_gap")
            return await original(*args, **kwargs)

        monkeypatch.setattr(world.platform.workspace, "complete_route", fail_once)
        world.models.script(
            step(text=json.dumps({"package": "support-desk", "entry": "triage", "message": None}))
        )
        body = RouteBody(text="工单", request_id="pg-restart")
        with pytest.raises(RuntimeUnavailableError):
            await world.platform.assistant.route(who, body)
        saved = (await world.platform.sessions.list_sessions(who))[0]
        replacement = AssistantRouter(world.platform, world.models)
        result = await replacement.route(who, body)
        assert result.session.session_id == saved.session_id
        assert len(await world.platform.sessions.list_sessions(who)) == 1
        assert len(world.models.requests) == 1
    finally:
        await world.platform.close()
