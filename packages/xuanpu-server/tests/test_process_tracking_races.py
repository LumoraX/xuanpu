"""进程启动与关闭交错时不能在释放执行空间后留下迟到进程。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import taifeng
from xuanpu_server.runtime.sandbox import ProcessTrackingExecutor


class Process:
    """可观察杀死和等待动作的最小进程协议。"""

    returncode = None

    def __init__(self):
        """尚未杀死，尚未等待。"""
        self.killed = self.waited = False

    def kill(self):
        """强杀后必须继续等待收尸。"""
        self.killed = True

    async def wait(self):
        """明确确认退出。"""
        self.waited = True
        return 0


async def test_close_waits_for_late_start_and_reaps_it():
    """关闭必须等迟到的启动退场，不能只关当前进程列表。"""
    entered, release = asyncio.Event(), asyncio.Event()
    process = Process()

    async def start(_):
        """在执行器底层启动中间暂停。"""
        entered.set()
        await release.wait()
        return process

    tracker = ProcessTrackingExecutor(SimpleNamespace(start=start))
    pending = asyncio.create_task(tracker.start(taifeng.CommandSpec("true", False, None, {})))
    await entered.wait()
    closing = asyncio.create_task(tracker.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(OSError):
        await pending
    await closing
    assert process.killed and process.waited


async def test_cancelling_start_reaps_process_created_after_cancellation():
    """调用者取消时底层启动仍可能成功，必须接住并终止它。"""
    entered, release = asyncio.Event(), asyncio.Event()
    process = Process()

    async def start(_):
        """底层正在创建进程。"""
        entered.set()
        await release.wait()
        return process

    tracker = ProcessTrackingExecutor(SimpleNamespace(start=start))
    pending = asyncio.create_task(tracker.start(taifeng.CommandSpec("true", False, None, {})))
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await tracker.close()
    assert process.killed and process.waited


async def test_stuck_start_does_not_skip_existing_process_cleanup(monkeypatch):
    """另一个启动挂住时，关闭失败也必须终止所有已知进程。"""
    from xuanpu_server.runtime import sandbox

    entered, release = asyncio.Event(), asyncio.Event()
    existing, late = Process(), Process()
    count = 0

    async def start(_):
        """先成功一个进程，再阻塞第二个启动。"""
        nonlocal count
        count += 1
        if count == 1:
            return existing
        entered.set()
        await release.wait()
        return late

    tracker = ProcessTrackingExecutor(SimpleNamespace(start=start))
    await tracker.start(taifeng.CommandSpec("true", False, None, {}))
    pending = asyncio.create_task(tracker.start(taifeng.CommandSpec("true", False, None, {})))
    await entered.wait()
    monkeypatch.setattr(sandbox, "KILL_WAIT_SECONDS", .02)
    try:
        with pytest.raises(Exception):
            await tracker.close()
        assert existing.killed and existing.waited
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await tracker.close()
    assert late.killed and late.waited
