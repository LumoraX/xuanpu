"""实际本机和Docker执行中，同员工不同会话及不同员工不能互读写。"""

from __future__ import annotations

import shlex

import pytest
import taifeng
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.tenancy import Principal


@pytest.mark.parametrize("world", ["local", "docker"], indirect=True)
async def test_each_employee_session_owns_files_and_connector_processes(world, step, read_turn):
    """同包同版本实际构建三个独立运行时，文件工具与执行器都隔离。"""
    await world.publish()
    people = []
    for user_id in ("usr_a", "usr_b"):
        await world.platform.identity.store.create_user(UserRecord(tenant_id="acme", user_id=user_id,
            display_name=user_id, created_at=1.0))
        people.append(Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id=user_id))
    sessions = []
    for who in (people[0], people[1], people[0]):
        session = await world.platform.sessions.create(who, package="support-desk")
        world.models.script(step(text="ready"))
        await read_turn(await world.platform.sessions.send_message(who, session.session_id, "hello"))
        sessions.append(session)
    runtimes = list(world.platform.runtimes._runtimes.values())
    assert len(runtimes) == 3
    assert len({runtime.sandbox.workspace for runtime in runtimes}) == 3
    assert len({runtime.connector_sandbox.workspace for runtime in runtimes}) == 3
    a, b, c = (runtime.sandbox for runtime in runtimes)
    await a.filesystem.write_bytes("only-a.txt", b"PRIVATE-A")
    for sibling in (b, c):
        assert not (await sibling.filesystem.metadata("only-a.txt")).exists
        process = await sibling.executor.start(taifeng.CommandSpec(
            command=shlex.join(("/bin/cat", str(a.workspace / "only-a.txt"))), shell=False, cwd=sibling.workspace_inside,
            env=sibling.process_env()))
        assert await process.wait() != 0
    await world.platform.sessions.close(people[0], sessions[0].session_id)
    assert not (await b.filesystem.metadata("only-a.txt")).exists
    await b.filesystem.write_bytes("still-alive.txt", b"yes")
    assert await b.filesystem.read_bytes("still-alive.txt") == b"yes"
