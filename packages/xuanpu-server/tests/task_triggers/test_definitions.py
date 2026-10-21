"""执行安排须本人确认，同请求和版本始终保护授权范围。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import ConflictError

from .conftest import request_for


async def test_confirmed_definition_is_atomic_and_replay_has_one_grant(trigger_world):
    """不创建伪占位任务，独立授权与定义一起提交。"""
    w = trigger_world
    assert hasattr(w.platform,'task_definitions'), 'R2-B尚无执行安排服务'
    service = w.platform.task_definitions
    service.config = service.config.model_copy(update={'enabled':True})
    service.now = lambda:w.now
    request = await request_for(w)
    first = await service.create(w.who,request)
    again = await service.create(w.who,request)
    assert first.definition_id == again.definition_id and first.version == 1
    async with w.platform.workspace.transaction(w.who,'work-task:trigger:inspect') as state:
        assert len(state.task_definitions)==1 and len(state.task_grants)==1
        assert not state.work_tasks
    with pytest.raises(ConflictError):
        await service.create(w.who,{**request,'title':'changed'})
    assert first.tenant_id == w.who.tenant_id



async def test_unconfirmed_and_stale_edit_cannot_change_authority(trigger_world):
    """未确认不能启用，修改必须匹配本人当前版本。"""
    w = trigger_world
    assert hasattr(w.platform,'task_definitions'), 'R2-B尚无执行安排服务'
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    request=await request_for(w)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        await service.create(w.who,{**request,'confirmed':False})
    first=await service.create(w.who,request)
    changed=await service.update(w.who,first.definition_id,{**request,'request_id':'edit','expected_version':1,'title':'新版'})
    assert changed.version==2 and changed.grant_id != first.grant_id
    with pytest.raises(ConflictError):
        await service.update(w.who,first.definition_id,{**request,'request_id':'stale','expected_version':1})
