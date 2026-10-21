"""主对话选择办公能力后必须先持久受理，重复请求不能创建另一项工作。"""

from __future__ import annotations

import asyncio
import json

from xuanpu_server.assistant import RouteBody


async def test_main_conversation_returns_persistent_task_not_unmanaged_session(office_world, step):
    """同一主对话请求及输入版本绑定同一个真实任务。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    file = await platform.workspace.upload(who,'数据.csv','部门,金额\nA,1.00\n'.encode(),'text/csv','source')
    gate = asyncio.Event()
    world.models.script(step(text=json.dumps({'package':'office-assistant','entry':'office','message':None})),
                        step(wait=gate))
    body = RouteBody.model_validate({'text':'按部门汇总材料','request_id':'chat-office',
        'attachments':[file['id']], 'input_files':[{'item_id':file['id'],'revision':1}]})
    first = await platform.assistant.route(who,body)
    second = await platform.assistant.route(who,body)
    assert first.work_task_id == second.work_task_id and first.work_task_id
    assert first.session is None
    tasks = await platform.work_tasks.store.list(who)
    assert len(tasks) == 1 and tasks[0].inputs[0].revision == 1
    gate.set()
    await asyncio.wait_for(platform.task_registry.join(), 10)
