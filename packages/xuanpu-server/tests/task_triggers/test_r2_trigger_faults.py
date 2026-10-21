"""R2-C关闭认领后仍可原生管理，重开仅处理原唯一发生实例。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import ConflictError

from .conftest import request_for


@pytest.mark.parametrize('gate',['trigger','durable','tenant_scope'])
async def test_forward_rollback_keeps_reader_pause_cancel_and_no_new_occurrence(trigger_world,gate):
    """先真实确认再关闭受理，不删除账本，不把旧R1 reader当作回退。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    record=await service.create(w.who,await request_for(w))
    if gate=='trigger':service.config=service.config.model_copy(update={'enabled':False})
    elif gate=='durable':service.tasks.durable=service.tasks.durable.model_copy(update={'enabled':False})
    else:service.config=service.config.model_copy(update={'tenant_ids':('another',)})
    w.now+=60
    assert await service.fire_time(w.who,record.definition_id) is None
    assert not await service.occurrences(w.who,record.definition_id)
    assert (await service.get(w.who,record.definition_id)).definition_id==record.definition_id
    paused=await service.act(w.who,record.definition_id,'pause',{'request_id':'rollback-pause','expected_version':record.state_version})
    with pytest.raises(ConflictError):
        await service.act(w.who,record.definition_id,'resume',{'request_id':'rollback-resume','expected_version':paused.state_version})
    cancelled=await service.act(w.who,record.definition_id,'cancel',{'request_id':'rollback-cancel','expected_version':paused.state_version})
    assert cancelled.cancelled and len(await service.list(w.who))==1
