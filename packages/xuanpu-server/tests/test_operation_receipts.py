"""内存和真实数据库对审批认领提供相同原子语义。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from xuanpu_server.controlplane.models import AuditQuery, AuditRecord, SessionRecord
from xuanpu_server.errors import ConflictError
from xuanpu_server.tenancy import Tenant


async def prepared(store: Any) -> Any:
    """创建有租户约束的回执测试会话。"""
    await store.create_tenant(Tenant("acme", "测试"))
    await store.create_session(SessionRecord("s", "acme", "office", "1", "main", 0, 0, "key:k"))
    return store.operation_receipts


async def test_concurrent_claim_executes_once(store: Any) -> None:
    """并发认领只有一个成功，且不因任意超时释放。"""
    receipts = await prepared(store)
    async def claim() -> Any:
        return await receipts.claim("acme", "s", {"r1": {"granted": True}}, at=1)
    results = await asyncio.gather(claim(), claim(), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    success = next(item for item in results if not isinstance(item, Exception))
    conflict = next(item for item in results if isinstance(item, ConflictError))
    assert conflict.operation_id == success.operation_id
    assert conflict.state == "claimed"
    with pytest.raises(ConflictError):
        await receipts.claim("acme", "s", {"r1": {"granted": True}}, at=999999)


async def test_submitted_replays_original_submission(store: Any) -> None:
    """重复相同决定返回原提交标识，不再提交内核。"""
    receipts = await prepared(store)
    first = await receipts.claim("acme", "s", {"r1": {"granted": True}}, at=1)
    await receipts.submitted("acme", first.operation_id, "sub1", at=2)
    replay = await receipts.claim("acme", "s", {"r1": {"granted": True}}, at=3)
    assert replay.replay and replay.submission_id == "sub1"
    with pytest.raises(ConflictError):
        await receipts.claim("acme", "s", {"r1": {"granted": False}}, at=3)


async def test_overlap_rolls_back_entire_batch(store: Any) -> None:
    """部分重叠不能认领剩余半组。"""
    receipts = await prepared(store)
    await receipts.claim("acme", "s", {"r1": {"granted": True}}, at=1)
    with pytest.raises(ConflictError):
        await receipts.claim("acme", "s", {"r1": True, "r2": True}, at=2)
    result = await receipts.claim("acme", "s", {"r2": True}, at=3)
    assert not result.replay


async def test_receipt_settlement_and_audit_share_transaction(store: Any) -> None:
    """核销回执有审计，已核销请求不再进入执行。"""
    receipts = await prepared(store)
    first = await receipts.claim("acme", "s", {"r1": {"granted": True}}, at=1)
    await receipts.submitted("acme", first.operation_id, "sub1", at=2)
    audit = AuditRecord("acme", "control", "operation_settled", 3, "system:receipts",
                        session_id="s", data={"operation_id": first.operation_id})
    await receipts.settle("acme", first.operation_id, "applied", audit, at=3)
    result = await receipts.claim("acme", "s", {"r1": {"granted": True}}, at=4)
    assert result.replay and result.state == "applied"
    records = await store.query_audit("acme", AuditQuery(kind="operation_settled"))
    assert len(records) == 1


async def test_unknown_result_is_never_automatically_retried(store: Any) -> None:
    """提交边界崩溃不允许以认领过期为由再次批准。"""
    receipts = await prepared(store)
    first = await receipts.claim("acme", "s", {"r1": True}, at=1)
    await receipts.settle("acme", first.operation_id, "outcome_unknown", None, at=2)
    with pytest.raises(ConflictError, match="核实"):
        await receipts.claim("acme", "s", {"r1": True}, at=999999)


async def test_proven_unsubmitted_claim_can_be_released(store: Any) -> None:
    """参数检查失败且从未提交时可以改正，不把审批永久锁死。"""
    receipts = await prepared(store)
    first = await receipts.claim("acme", "s", {"r1": True}, at=1)
    await receipts.release("acme", first.operation_id)
    result = await receipts.claim("acme", "s", {"r1": False}, at=2)
    assert not result.replay


async def test_rejected_receipt_survives_crash_before_release(store: Any) -> None:
    """拒绝已持久化但清理尚未运行时，修正决定仍能认领。"""
    receipts = await prepared(store)
    first = await receipts.claim("acme", "s", {"r1": True}, at=1)
    await receipts.submitted("acme", first.operation_id, "sub1", at=2)
    await receipts.settle("acme", first.operation_id, "rejected", None, at=3)
    corrected = await receipts.claim("acme", "s", {"r1": False}, at=4)
    assert not corrected.replay and corrected.operation_id != first.operation_id


async def test_receipt_isolation_and_audit_failure_leave_claim_unchanged(store: Any) -> None:
    """不同租户同名待办可各自认领，错误租户审计不能结算任何记录。"""
    receipts = await prepared(store)
    await store.create_tenant(Tenant("other", "另一个租户"))
    await store.create_session(SessionRecord("s", "other", "office", "1", "main", 0, 0, "key:k"))
    first = await receipts.claim("acme", "s", {"r1": True}, at=1)
    other = await receipts.claim("other", "s", {"r1": False}, at=1)
    assert first.operation_id != other.operation_id
    bad = AuditRecord("other", "control", "operation_settled", 2, "system:test")
    with pytest.raises(ValueError, match="租户"):
        await receipts.settle("acme", first.operation_id, "applied", bad, at=2)
    assert await store.query_audit("other", AuditQuery(kind="operation_settled")) == []
    await receipts.release("acme", first.operation_id)
    assert not (await receipts.claim("acme", "s", {"r1": False}, at=3)).replay
    with pytest.raises(ConflictError):
        await receipts.claim("other", "s", {"r1": False}, at=3)


async def test_release_cannot_make_applied_decision_executable_again(store: Any) -> None:
    """清理只认领或拒绝项，已应用决定绝不能释放成一次新写入。"""
    receipts = await prepared(store)
    first = await receipts.claim("acme", "s", {"r1": True}, at=1)
    await receipts.submitted("acme", first.operation_id, "sub1", at=2)
    await receipts.settle("acme", first.operation_id, "applied", None, at=3)
    await receipts.release("acme", first.operation_id)
    replay = await receipts.claim("acme", "s", {"r1": True}, at=4)
    assert replay.replay and replay.submission_id == "sub1"


async def test_audit_storage_failure_rolls_back_settlement(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """实际数据库审计写入失败后，回执和审计均不留下半次提交。"""
    from xuanpu_server.runtime.receipt_store import MemoryReceiptStore
    receipts = await prepared(store)
    first = await receipts.claim("acme", "s", {"r1": True}, at=1)
    await receipts.submitted("acme", first.operation_id, "sub1", at=2)
    audit = AuditRecord("acme", "control", "operation_settled", 3, "system:test", session_id="s")
    if isinstance(receipts, MemoryReceiptStore):
        original = receipts._audit
        def fail(record: Any) -> None:
            raise RuntimeError("R0-audit-failure")
        monkeypatch.setattr(receipts, "_audit", fail)
    else:
        async with receipts._pool.acquire() as conn:
            await conn.execute("CREATE FUNCTION pg_temp.r0_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'R0-audit-failure'; END $$")
            # 测试对象的审计表已通过 SqlNamespace 校验。
            await conn.execute(f"CREATE TRIGGER r0_audit_failure BEFORE INSERT ON {receipts._audit} "
                "FOR EACH ROW EXECUTE FUNCTION pg_temp.r0_audit_failure()")
    with pytest.raises(Exception, match="R0-audit-failure"):
        await receipts.settle("acme", first.operation_id, "applied", audit, at=3)
    replay = await receipts.claim("acme", "s", {"r1": True}, at=4)
    assert replay.state == "submitted"
    assert await store.query_audit("acme", AuditQuery(kind="operation_settled")) == []
    if isinstance(receipts, MemoryReceiptStore):
        monkeypatch.setattr(receipts, "_audit", original)
    else:
        async with receipts._pool.acquire() as conn:
            await conn.execute(f"DROP TRIGGER r0_audit_failure ON {receipts._audit}")
    await receipts.settle("acme", first.operation_id, "applied", audit, at=5)
    assert len(await store.query_audit("acme", AuditQuery(kind="operation_settled"))) == 1
