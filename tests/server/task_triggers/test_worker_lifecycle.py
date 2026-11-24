"""先停止触发，再停止派发与执行，特性关闭保留管理能力。"""
from __future__ import annotations

from .conftest import request_for


async def test_platform_closes_trigger_worker_before_task_registry(trigger_world,monkeypatch):
    """关闭顺序实测，避免任务执行器关闭后仍被触发扫描新受理。"""
    w=trigger_world
    order=[]
    tc=w.platform.trigger_worker.close
    rc=w.platform.task_registry.close
    async def trigger_close():
        order.append('trigger')
        await tc()
    async def registry_close():
        order.append('registry')
        await rc()
    monkeypatch.setattr(w.platform.trigger_worker,'close',trigger_close)
    monkeypatch.setattr(w.platform.task_registry,'close',registry_close)
    await w.platform.close()
    assert order[:2]==['trigger','registry']


async def test_disabled_feature_still_allows_read_pause_and_cancel(trigger_world):
    """开关不等于丢弃既有定义，旧事件和实例仍可追溯。"""
    w=trigger_world
    s=w.platform.task_definitions
    s.now=lambda:w.now
    s.config=s.config.model_copy(update={'enabled':True})
    record=await s.create(w.who,await request_for(w))
    s.config=s.config.model_copy(update={'enabled':False})
    assert (await s.get(w.who,record.definition_id)).definition_id==record.definition_id
    paused=await s.act(w.who,record.definition_id,'pause',{'request_id':'off-pause','expected_version':1})
    cancelled=await s.act(w.who,record.definition_id,'cancel',{'request_id':'off-cancel','expected_version':paused.state_version})
    assert cancelled.cancelled
    w.now+=100
    await w.platform.trigger_worker.tick_time()
    assert not await s.tasks.store.list(w.who)
