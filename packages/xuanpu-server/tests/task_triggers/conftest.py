"""触发测试复用R2-A真实双后端身份、包与对象存储。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ..work_tasks.conftest import office_image, recovery_world  # noqa: F401


@pytest.fixture
async def trigger_world(recovery_world):  # noqa: F811
    """停止自动执行后用注入时钟驱动同一服务，不另建任务库。"""
    h = recovery_world
    await h.platform.task_registry.close()
    h.platform.work_tasks.executor = None
    w = SimpleNamespace(harness=h,platform=h.platform,who=h.who,now=1792587600.)
    h.platform.workspace._now = lambda:w.now
    h.platform.work_tasks._now = lambda:w.now
    yield w


async def request_for(w, *, kind='once'):
    """范围和参数均来自真实上传的本人材料。"""
    source = await w.platform.workspace.upload(w.who,'合成输入.csv','部门,金额\nA,12.50\nB,3.00\n'.encode(),
                                                'text/csv','trigger-source')
    return {'request_id':'definition-create','confirmed':True,'title':'本人汇总',
        'template':{'kind':'office','text':'按部门汇总金额，没有币种列',
                    'input_files':[{'item_id':source['id'],'revision':1}]},
        'trigger':{'kind':'time','repeat':kind,'at':'2026-10-21T21:01:00+08:00'},
        'timezone':'Asia/Shanghai','valid_until':'2026-11-01T00:00:00+08:00',
        'max_executions':3,'max_attempts':3,'max_tokens':300000}
