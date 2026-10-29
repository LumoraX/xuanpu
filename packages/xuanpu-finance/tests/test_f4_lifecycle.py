"""原生通知任务的模式、故障与取消不会代替业务状态。"""
from __future__ import annotations

import asyncio
from typing import Any

from xuanpu_finance.api import create_app

from .test_f1_api import Auth


class NoticeProbe:
    """只计本地扫描次数，不包含任何外部执行能力。"""

    def __init__(self, *, fail: bool = False) -> None:
        """显式注入一次故障用于验证生命周期。"""
        self.calls,self.fail=0,fail

    async def run_once(self) -> int:
        """模拟扫描入口，故障必须在运行状态可见。"""
        self.calls+=1
        if self.fail:
            raise RuntimeError('isolated failure')
        return 0


async def test_notice_disabled_then_enabled_and_cancelled() -> None:
    """停用不生成提醒；启用后扫描；退出等待任务取消完成。"""
    worker=NoticeProbe()
    app=create_app(store=Any,auth=Auth(),mode='disabled',notification_worker=worker)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(.01)
        assert worker.calls==0
        app.state.mode='enabled'
        async with asyncio.timeout(2):
            while not worker.calls:
                await asyncio.sleep(.01)
    assert app.state.notification_task.done()


async def test_notice_fault_is_visible_and_never_sets_business_result() -> None:
    """派生提醒故障单独可见，不伪造业务回执或改变模式。"""
    app=create_app(store=Any,auth=Auth(),notification_worker=NoticeProbe(fail=True))
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(2):
            while app.state.notification_worker_error is None:
                await asyncio.sleep(.01)
        assert app.state.notification_worker_error=='RuntimeError'
        assert app.state.worker_error is None and app.state.mode=='enabled'
