"""公开会话接口冷恢复保留批准身份；这不授予文件依赖调用恢复资格。"""
from __future__ import annotations

from typing import Any


async def test_public_cold_resume_preserves_pending_and_decision(world: Any, step: Any, read_turn: Any) -> None:
    """关闭整个平台再恢复同一线程，原批准标识只执行一次。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package='support-desk')
    world.models.script(step(calls=[('cold-call', 'mcp__crm__update_ticket', {'id': 'T-R2'})]),
                        step(text='已处理'))
    await read_turn(await world.platform.sessions.send_message(world.member, session.session_id, '修改'))
    pending = await world.platform.sessions.pending(world.member, session.session_id)
    assert len(pending) == 1 and not world.calls()
    before = await world.platform.store.get_session('acme', session.session_id)
    await world.platform.close()
    reopened = world.sibling('r2-contract', data_dir=world.data_dir)
    try:
        await reopened.start()
        assert await reopened.sessions.pending(world.member, session.session_id) == pending
        decision = {pending[0]['request_id']: {'granted': True}}
        turn = await reopened.sessions.resume(world.member, session.session_id, decision)
        await read_turn(turn)
        repeated = await reopened.sessions.resume(world.member, session.session_id, decision)
        assert repeated.replayed and repeated.submission_id == turn.submission_id
        assert len(world.calls()) == 1
        after = await reopened.store.get_session('acme', session.session_id)
        assert after.thread_id == before.thread_id
    finally:
        await reopened.close()
