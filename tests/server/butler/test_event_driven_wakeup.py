"""事件驱动唤醒总线、定点推进与条件超时器测试（ADR 0024）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.engine.butler.wake_bus import (
    ConditionalTimerRegistry,
    MemoryWakeBus,
    WakeSignal,
    decode_signal,
    encode_signal,
)
from server.engine.tracing.models import TraceOwner

from .conftest import enable

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")


def test_signal_encode_decode_and_validation() -> None:
    """合法信号可紧凑编解码；非法类型或超长载荷被拒绝或安全忽略。"""
    sig = WakeSignal(tenant_id="acme", kind="dispatch", target_id="dsp_123")
    raw = encode_signal(sig)
    assert raw == '{"t":"acme","k":"dispatch","id":"dsp_123"}'
    assert decode_signal(raw) == sig

    with pytest.raises(ValueError):
        WakeSignal(tenant_id="acme", kind="unknown", target_id="dsp_1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WakeSignal(tenant_id="bad tenant!", kind="dispatch", target_id="dsp_1")

    assert decode_signal("") is None
    assert decode_signal("not-json") is None
    assert decode_signal("[]") is None
    assert decode_signal('{"t":"acme","k":"dispatch"}') is None
    assert decode_signal('{"t":"acme","k":"dispatch","id":"' + "x" * 600 + '"}') is None


async def test_memory_wake_bus_coalesces_duplicates() -> None:
    """同路由键的连续唤醒信号自动合并去重，不同目标保持先后顺序。"""
    bus = MemoryWakeBus()
    s1 = WakeSignal(tenant_id="acme", kind="dispatch", target_id="dsp_1")
    s2 = WakeSignal(tenant_id="acme", kind="group", target_id="grp_1")
    await bus.publish(s1)
    await bus.publish(s1)
    await bus.publish(s2)
    await bus.publish(s1)

    batch = await bus.drain(timeout=0.1)
    assert batch == (s1, s2)
    assert await bus.drain(timeout=0.01) == ()


async def test_conditional_timer_cancel_and_fire() -> None:
    """条件超时器：提前取消不触发，到期未取消按时触发。"""
    registry = ConditionalTimerRegistry()
    fired: list[str] = []

    async def mark(name: str) -> None:
        """记录被触发的超时器名。"""
        fired.append(name)

    registry.arm("dispatch:d1", deadline=10.0, callback=lambda: mark("d1"))
    registry.arm("dispatch:d2", deadline=20.0, callback=lambda: mark("d2"))
    assert registry.next_wait(now=5.0, max_wait=30.0) == 5.0
    assert registry.cancel("dispatch:d1") is True
    assert registry.cancel("dispatch:d1") is False
    assert registry.next_wait(now=5.0, max_wait=30.0) == 15.0

    assert await registry.fire_due(now=15.0) == 0
    assert await registry.fire_due(now=20.0) == 1
    assert fired == ["d2"]
    assert len(registry) == 0


async def test_event_driven_wakeup_delivers_without_polling_wait(
    butler: dict[str, Any], step: Any,
) -> None:
    """对账兜底周期设为 60 秒时，会话匠人结束后仍经事件快路径毫秒级唤醒交付并取消超时器。"""
    enable(butler["world"], wake_interval_seconds=60.0, reconcile_interval_seconds=60.0)
    platform = butler["world"].platform
    wakeup = platform.butler.wakeup
    timed_out: list[str] = []

    async def on_timeout() -> None:
        """超时回调：提前交付后不应被触发。"""
        timed_out.append("expired")

    butler["world"].models.script(
        step(calls=[("c1", "dispatch_artisan", {
            "package": "support-desk", "entry": "triage", "instruction": "核对工单 #42",
        })]),
        step(text="已交给客服匠人。"),
        step(text="工单 #42 已处理完毕。"),
        step(text="已为您办妥：工单 #42 已处理完毕。"),
    )
    wakeup.start()
    try:
        started_at = time.monotonic()
        resp = await butler["http"].post(
            "/v1/assistant/messages", json={"request_id": "ev1", "text": "帮我查工单 42"})
        assert resp.status_code == 200, resp.text
        trace_id = resp.json()["trace_id"]
        [record] = await platform.store.dispatch_store.list_for_trace(ALICE, trace_id)
        wakeup.arm_deadline(f"dispatch:{record.dispatch_id}", time.time() + 120.0, on_timeout)

        async with asyncio.timeout(5.0):
            while True:
                cur = await platform.dispatches.get(ALICE, record.dispatch_id)
                if cur.state == "delivered":
                    break
                await asyncio.sleep(0.02)
        elapsed = time.monotonic() - started_at
        assert elapsed < 5.0
        # 交付时自动取消对应条件超时器
        assert wakeup.cancel_deadline(f"dispatch:{record.dispatch_id}") is False
        assert timed_out == []
    finally:
        await wakeup.close()


async def test_work_task_transition_publishes_wake_signal(butler: dict[str, Any]) -> None:
    """工作任务状态流转进入终态或 waiting_user 时通过监听器向 WakeBus 发射任务信号。"""
    platform = butler["world"].platform
    received: list[WakeSignal] = []

    class _SpyBus(MemoryWakeBus):
        """记录所有发布信号的测试总线。"""

        async def publish(self, signal: WakeSignal) -> None:
            received.append(signal)
            await super().publish(signal)

    spy = _SpyBus()
    platform.butler.wake_bus = spy

    from server.domains.work_tasks.models import WorkTaskRecord

    def _rec(status: str) -> WorkTaskRecord:
        return WorkTaskRecord(
            task_id="tsk_evt_1", tenant_id="acme", user_id="usr_alice", actor="usr_alice",
            request_id="req_evt_1", input_fingerprint="a" * 64, inputs=(),
            package="support-desk", package_version="1.0.0", package_digest="b" * 64,
            entry="triage", text="汇总", timezone="Asia/Shanghai", process_owner="node-1",
            status=status, state_version=2, accepted_at=1.0, updated_at=2.0,  # type: ignore[arg-type]
        )

    await platform.butler._on_work_task_transition(_rec("running"))
    assert received == []
    await platform.butler._on_work_task_transition(_rec("waiting_user"))
    await platform.butler._on_work_task_transition(_rec("succeeded"))
    assert [s.kind for s in received] == ["task", "task"]
    assert [s.target_id for s in received] == ["tsk_evt_1", "tsk_evt_1"]
