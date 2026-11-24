"""端到端：租户开启追踪后，员工会话的一轮对话经事件枢纽落成一条持久 trace。"""

from __future__ import annotations

from dataclasses import replace

from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.engine.tracing.config import ExecutionTraceConfig
from server.engine.tracing.models import TraceOwner


async def _employee(world, user_id: str) -> Principal:
    """建一名员工并以本人身份调用。"""
    await world.platform.identity.store.create_user(UserRecord(
        tenant_id="acme", user_id=user_id, display_name=user_id, created_at=1.0))
    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id=user_id)


def _enable(world, *tenants: str) -> None:
    """测试内替换平台默认参数：只影响之后新建的引擎。"""
    runtimes = world.platform.runtimes
    runtimes._defaults = replace(  # noqa: SLF001 —— 测试内装配
        runtimes.defaults, execution_trace=ExecutionTraceConfig(enabled=True, tenants=tenants))


async def test_employee_turn_becomes_a_trace(world, step, read_turn) -> None:
    """一轮对话 → 一条 trace：根执行 span 指向会话，用量完整，记录完整。"""
    _enable(world, "acme")
    await world.publish()
    alice = await _employee(world, "usr_alice")
    session = await world.platform.sessions.create(alice, package="support-desk")
    world.models.script(step(text="收到"))
    await read_turn(await world.platform.sessions.send_message(alice, session.session_id, "你好"))
    await world.platform.sessions.close(alice, session.session_id)
    store = world.platform.store.trace_store
    owner = TraceOwner(tenant_id="acme", user_id="usr_alice")
    page = await store.list(owner)
    assert page.total == 1
    view = await store.get(owner, page.items[0].trace_id)
    roots = [span for span in view.spans if span.parent_span_id is None]
    assert len(roots) == 1 and roots[0].ref == session.session_id
    assert roots[0].state == "completed" and roots[0].usage_complete
    assert not view.trace.incomplete
    bob = TraceOwner(tenant_id="acme", user_id="usr_bob")
    assert (await store.list(bob)).total == 0


async def test_disabled_tenant_and_key_sessions_are_not_traced(world, step, read_turn) -> None:
    """未开启的租户、没有主人的 API key 会话都不产生记录。"""
    _enable(world, "globex")
    await world.publish()
    alice = await _employee(world, "usr_alice")
    for who in (alice, world.member):
        session = await world.platform.sessions.create(who, package="support-desk")
        world.models.script(step(text="收到"))
        await read_turn(await world.platform.sessions.send_message(who, session.session_id, "你好"))
    owner = TraceOwner(tenant_id="acme", user_id="usr_alice")
    assert (await world.platform.store.trace_store.list(owner)).total == 0
