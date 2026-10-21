"""已有主动识别只导出带证据草稿，不能自动赋予执行权限。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import ConflictError


async def test_evidenced_draft_is_read_only_and_requires_fresh_confirmation(trigger_world):
    """真实规则跟进有来源；生成草稿不改提醒、定义或任务。"""
    w=trigger_world
    proactive=w.platform.proactive
    proactive.now=lambda:w.now
    item=await w.platform.workspace.create(w.who,'todo',{'title':'核对报价','due_at':w.now-1},'draft-todo')
    await proactive.scan_owner('acme',w.who.user_id)
    record=(await proactive.list(w.who))[0]
    assert hasattr(proactive,'execution_draft'),'主动业务尚无只读执行草稿'
    draft=await proactive.execution_draft(w.who,record['id'],record['version'])
    assert draft['confirmed'] is False and draft['source_version']==record['version']
    assert draft['evidence_refs']==[f"item:{item['id']}:{item['version']}"]
    assert draft['template']['text']=='核对报价' and 'trigger' not in draft
    assert (await proactive.get(w.who,record['id']))==record
    assert await w.platform.task_definitions.list(w.who)==[]
    assert await w.platform.work_tasks.store.list(w.who)==[]
    with pytest.raises(ConflictError):
        await proactive.execution_draft(w.who,record['id'],record['version']+1)
