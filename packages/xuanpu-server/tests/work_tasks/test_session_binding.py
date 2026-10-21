"""任务会话的执行关联只由服务器建立，不能通过普通会话接口旁路执行。"""

from __future__ import annotations

import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.models import WorkTaskInput


async def test_task_session_rejects_unmanaged_send_and_resume(task_api):
    """有内容权限也不能绕过任务托管者提交另一轮执行。"""
    platform = task_api.world.platform
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    file = await platform.workspace.upload(who, "a.txt", b"minutes", "text/plain", "input")
    task = await platform.work_tasks.accept(who, WorkTaskInput.model_validate({"request_id": "r",
        "text": "整理", "input_files": [{"item_id": file["id"], "revision": 1}]}))
    session = await platform.sessions.create(who, package="office-assistant", version="1.0.0",
        entry="triage", request_id="task:" + task.task_id, work_task_id=task.task_id)
    assert session.work_task_id == task.task_id
    with pytest.raises(ConflictError):
        await platform.sessions.send_message(who, session.session_id, "extra")
    with pytest.raises(ConflictError):
        await platform.sessions.resume(who, session.session_id, {"r": {"granted": True}})
    with pytest.raises(ConflictError):
        await platform.sessions.create(who, package="office-assistant", version="1.0.0", entry="triage",
            request_id="task:" + task.task_id)


async def test_public_metadata_cannot_choose_execution_scope(task_api, step, read_turn):
    """业务metadata看起来像任务标识也不会改变真实执行边界。"""
    platform = task_api.world.platform
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    session = await platform.sessions.create(who, package="office-assistant", metadata={"work_task_id": "forged"})
    assert session.work_task_id is None
    task_api.world.models.script(step(text="ready"))
    await read_turn(await platform.sessions.send_message(who, session.session_id, "hello"))
    runtime = platform.sessions._live[("acme", session.session_id)].runtime
    from xuanpu_server.runtime.execution_scope import ExecutionScope

    assert runtime.key[3] == ExecutionScope("acme", "usr_a", "session:" + session.session_id).storage_key()
