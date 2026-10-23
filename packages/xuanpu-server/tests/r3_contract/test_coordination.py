"""协调事务在两个真实后端上保持回滚、取消、锁序和逐条确认语义。"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
async def coordinator(r3_world, r3_http):
    """使用完整平台装配及已存在的员工，而非独立模拟仓储。"""
    assert hasattr(r3_world.platform, 'coordination'), '平台尚未装配跨员工协调事务'
    return r3_world.platform.coordination


async def test_exception_rolls_back_receipt_and_event(coordinator):
    """业务失败不能遗留成功回执或待发送通知。"""
    with pytest.raises(RuntimeError, match='业务失败'):
        async with coordinator.transaction('acme', participants=('usr_alice',)) as tx:
            tx.record_receipt('usr_alice', 'request-1', {'action': 'create'}, {'id': 'one'})
            tx.emit('membership_changed', 'one', 1, actor='usr_alice', at=1)
            raise RuntimeError('业务失败')
    async with coordinator.transaction('acme') as tx:
        assert tx.receipt('usr_alice', 'request-1', {'action': 'create'}) is None
        assert tx.pending_events('consumer') == []


async def test_receipt_conflicts_and_event_ack_is_individual(coordinator):
    """同键重放固定结果；确认较新事件不跳过未确认的较早事件。"""
    from xuanpu_server.errors import ConflictError

    async with coordinator.transaction('acme') as tx:
        tx.record_receipt('usr_alice', 'r1', {'x': 1}, {'id': 'original'})
        first = tx.emit('role_revoked', 'role-a', 2, actor='usr_alice', at=1)
        second = tx.emit('role_revoked', 'role-b', 2, actor='usr_alice', at=2)
        assert tx.emit('role_revoked', 'role-a', 2, actor='usr_alice', at=1) == first
        tx.ack('consumer', second.id)
    async with coordinator.transaction('acme') as tx:
        assert tx.receipt('usr_alice', 'r1', {'x': 1}) == {'id': 'original'}
        with pytest.raises(ConflictError):
            tx.receipt('usr_alice', 'r1', {'x': 2})
        assert [e.id for e in tx.pending_events('consumer')] == [first.id]
        assert len(tx.pending_events('other-consumer')) == 2


async def test_reverse_participant_order_does_not_deadlock(coordinator):
    """并发相反输入顺序仍统一按员工ID取锁。"""
    async def write(participants, key):
        """模拟两个并发业务事务。"""
        async with coordinator.transaction('acme', participants=participants) as tx:
            assert tx.participants == ('usr_alice', 'usr_bob')
            tx.record_receipt('usr_alice', key, {}, {'ok': True})
    async with asyncio.timeout(5):
        await asyncio.gather(write(('usr_bob', 'usr_alice'), 'a'),
                             write(('usr_alice', 'usr_bob'), 'b'))


async def test_cancellation_rolls_back(coordinator):
    """被取消的事务没有持久副作用，后续事务可以正常进入。"""
    entered = asyncio.Event()
    async def write():
        """在提交前等待取消。"""
        async with coordinator.transaction('acme', participants=('usr_alice',)) as tx:
            tx.record_receipt('usr_alice', 'cancelled', {}, {'ok': True})
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(write())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with coordinator.transaction('acme') as tx:
        assert tx.receipt('usr_alice', 'cancelled', {}) is None


async def test_missing_or_disabled_participant_is_denied(coordinator, r3_world):
    """当前员工状态在事务内检查，不依赖登录时的缓存。"""
    from xuanpu_server.errors import PermissionDeniedError

    for user in ('usr_absent', 'usr_bob'):
        if user == 'usr_bob':
            await r3_world.platform.identity.store.set_user_status(
                'acme', user, 'disabled', reason='admin', at=10)
        with pytest.raises(PermissionDeniedError):
            async with coordinator.transaction('acme', participants=(user,)):
                pytest.fail('失效员工进入了事务')


async def test_coordination_timeout_releases_lock(coordinator):
    """短事务超时回滚；不会占住下一次请求。"""
    with pytest.raises(TimeoutError):
        async with coordinator.transaction('acme', timeout=0.02) as tx:
            tx.record_receipt('usr_alice', 'timeout', {}, {})
            await asyncio.sleep(1)
    async with coordinator.transaction('acme') as tx:
        assert tx.receipt('usr_alice', 'timeout', {}) is None


async def test_committed_receipts_cannot_be_rewritten(coordinator):
    """已提交回执和事件不可改写；两个后端不能出现静默分歧。"""
    from xuanpu_server.errors import ConflictError

    async with coordinator.transaction('acme') as tx:
        tx.record_receipt('usr_alice', 'immutable', {}, {'id': 'original'})
    with pytest.raises(ConflictError):
        async with coordinator.transaction('acme') as tx:
            tx.state.receipts.clear()
    async with coordinator.transaction('acme') as tx:
        assert tx.receipt('usr_alice', 'immutable', {}) == {'id': 'original'}
