"""领域事件可复用真实只读能力回执，不要求伪造办公文件。"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from ..work_tasks.test_capability_tasks import capability_world  # noqa: F401


async def test_todo_completion_runs_one_readonly_capability(capability_world,step):  # noqa: F811
    """原生待办完成事务触发，真实工具回执证明成功，重复扫描不调用第二次。"""
    world=capability_world
    service=world.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    todo=await world.platform.workspace.create(world.employee,'todo',{'title':'核实客户'},'trigger-todo')
    deadline=datetime.fromtimestamp(int(time.time())+3600,UTC).isoformat()
    record=await service.create(world.employee,{'request_id':'capability-trigger','confirmed':True,'title':'查询客户',
        'template':{'kind':'capability','text':'查询合成客户','capability':{'package':'support-desk','entry':'triage'}},
        'trigger':{'kind':'event','event_type':'workspace.todo_completed','subject_id':todo['id']},
        'timezone':'UTC','valid_until':deadline})
    world.models.script(step(calls=[('read','mcp__crm__search_customer',{'query':'C-R2B'})]),step(text='已读取客户'))
    await world.platform.workspace.update(world.employee,todo['id'],{'status':'done'},todo['version'])
    # PG事务时钟与应用扫描时钟存在毫秒差，后台领取也可能先于本次显式扫描。
    # 等待可观测的任务受理，不能把两次立即扫描当成异步工作已结束。
    tasks=[]
    async with asyncio.timeout(15):
        while not tasks:
            await world.platform.trigger_worker.tick_events()
            await asyncio.wait_for(world.platform.task_registry.join(),15)
            tasks=await service.tasks.store.list(world.employee)
            if not tasks:
                await asyncio.sleep(.02)
    for _ in range(2):
        await world.platform.trigger_worker.tick_events()
        await asyncio.wait_for(world.platform.task_registry.join(),15)
    tasks=await service.tasks.store.list(world.employee)
    assert len(tasks)==1 and tasks[0].status=='succeeded' and not tasks[0].artifacts,tasks
    assert tasks[0].definition_id==record.definition_id
    assert tasks[0].result_receipt['tool_receipts'][0]['call_id']=='read'
    assert len(world.calls())==1
