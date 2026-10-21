"""对话只提出服务端核定草稿，确认按钮才有独立授权。"""
from __future__ import annotations

import json

from xuanpu_server.assistant import RouteBody

from .conftest import request_for


async def test_server_preview_has_no_task_grant_or_notification(trigger_world):
    """预览核对实际发布能力和材料，返回固定范围但不产生任何执行。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    body=await request_for(w)
    assert hasattr(service,'preview'),'尚无只读安排预览'
    draft=await service.preview(w.who,{**body,'confirmed':False})
    assert draft['request']['confirmed'] is False
    assert draft['display']['files'][0]['name']=='合成输入.csv'
    async with service.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        assert not state.task_definitions and not state.task_grants and not state.work_tasks


async def test_assistant_schedule_proposal_never_becomes_immediate_office_task(trigger_world,step):
    """同一已确认对话请求回放草稿；没有点击确认无定义/任务/运行会话。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    w.platform.assistant._now=lambda:w.now
    source=await request_for(w)
    w.harness.world.models.script(step(text=json.dumps({'package':'@task-definitions','entry':'create','message':None,
        'task_definition':{'title':'稍后汇总','text':'按部门汇总金额','trigger':source['trigger'],
                           'timezone':'Asia/Shanghai','valid_until':source['valid_until']}})))
    body=RouteBody.model_validate({'text':'稍后替我汇总表格，不是提醒我','request_id':'schedule-chat',
        'input_files':source['template']['input_files']})
    outcome=await w.platform.assistant.route(w.who,body)
    assert getattr(outcome,'task_definition_draft',None),'尚无执行安排草稿'
    assert outcome.work_task_id is None and outcome.task_definition_draft['request']['confirmed'] is False
    again=await w.platform.assistant.route(w.who,body)
    assert again.task_definition_draft==outcome.task_definition_draft
    assert not await service.list(w.who) and not await service.tasks.store.list(w.who)


async def test_tenant_canary_cannot_preview_outside_allowlist(trigger_world):
    """部署配置的白名单不能被员工或模型草稿扩大。"""
    import pytest
    from xuanpu_server.errors import ConflictError
    from xuanpu_server.task_triggers.models import TriggerConfig
    w=trigger_world
    service=w.platform.task_definitions
    service.config=TriggerConfig(enabled=True,tenant_ids=['another'])
    body=await request_for(w)
    with pytest.raises(ConflictError):
        await service.preview(w.who,{**body,'confirmed':False})
    assert not await service.list(w.who)
