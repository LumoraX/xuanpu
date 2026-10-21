"""会议建议须引用原文，只有本人明确选择的行动项进入同一原生事务。"""

from __future__ import annotations

import pytest
from xuanpu_server.errors import ConflictError, InvalidRequestError
from xuanpu_server.work_tasks.decisions import OfficeActionsDecision
from xuanpu_server.work_tasks.models import ArtifactReceipt, InputFile
from xuanpu_server.work_tasks.proposals import ActionProposalService
from xuanpu_server.work_tasks.store import WorkTaskStore


async def prepared(env):
    """建立已核验纪要文件、但尚待本人确认的任务。"""
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who, env.record)
    task = await store.transition(env.who, task.task_id, 'running', expected_version=1, at=1001.0,
        artifacts=(ArtifactReceipt(item_id=env.file['id'], revision=1, sha256=env.record.inputs[0].sha256,
            size=15, mime_type='text/plain', input_refs=(InputFile(item_id=env.file['id'], revision=1),),
            generated_at=1001.0),), outputs_verified=True)
    service = ActionProposalService(env.workspace, store, now=lambda:1002.0)
    entries = await service.prepare(env.who, task, [{'kind':'todo', 'title':'整理需求', 'owner':'self',
        'source': {'input_name':'input_1.txt', 'start':0, 'end':7, 'quote':'private'}},
        {'kind':'todo', 'title':'未明确负责人', 'owner':'unresolved',
        'source': {'input_name':'input_1.txt', 'start':8, 'end':15, 'quote':'minutes'}}],
        {'input_1.txt':'private minutes'})
    current = await store.get(env.who, task.task_id)
    task = await store.transition(env.who, task.task_id, 'waiting_user', expected_version=current.state_version, at=1002.0)
    return service, task, entries


async def test_selected_actions_and_duplicate_confirm_commit_once(task_env):
    """只创建所选本人事项，网络重试返回同一持久回执。"""
    env = task_env
    service, task, entries = await prepared(env)
    decision = OfficeActionsDecision.model_validate({'kind':'office_actions','request_id':'choose',
        'state_version':task.state_version, 'choices':[{'action_id':entries[0]['action_id'], 'accepted':True,
                                                     'title':'我确认的标题'}]})
    result = await service.confirm(env.who, task.task_id, decision)
    again = await service.confirm(env.who, task.task_id, decision)
    assert result == again and result.status == 'succeeded'
    todos = await env.workspace.list(env.who, kind='todo')
    assert [item['data']['title'] for item in todos] == ['我确认的标题']
    assert len(result.action_results) == 2


async def test_source_update_invalidates_old_confirmation(task_env):
    """候选引用的文件已有新版本时不能继续创建旧建议。"""
    env = task_env
    service, task, entries = await prepared(env)
    await env.workspace.replace_content(env.who, env.file['id'], b'new decision', 'text/plain',
                                        env.file['version'], 'replace')
    decision = OfficeActionsDecision(kind='office_actions', request_id='old', state_version=task.state_version)
    with pytest.raises(ConflictError):
        await service.confirm(env.who, task.task_id, decision)
    assert await env.workspace.list(env.who, kind='todo') == []


async def test_invalid_choice_rolls_back_entire_batch(task_env):
    """先选有效项再选负责人未核定项，不能留下前半批待办。"""
    env = task_env
    service, task, entries = await prepared(env)
    decision = OfficeActionsDecision.model_validate({'kind':'office_actions','request_id':'bad',
        'state_version':task.state_version,'choices':[
            {'action_id':entry['action_id'], 'accepted':True} for entry in entries]})
    with pytest.raises(InvalidRequestError):
        await service.confirm(env.who, task.task_id, decision)
    assert await env.workspace.list(env.who, kind='todo') == []
    assert (await service.store.get(env.who, task.task_id)).status == 'waiting_user'


async def test_same_request_cannot_change_omitted_time_into_explicit_clear(task_env):
    """省略表示继承、显式null表示清空，两种决定不能共用同一幂等标识。"""
    service, task, entries = await prepared(task_env)
    payload = {'kind':'office_actions','request_id':'presence','state_version':task.state_version,
               'choices':[{'action_id':entries[0]['action_id'],'accepted':True}]}
    await service.confirm(task_env.who,task.task_id,OfficeActionsDecision.model_validate(payload))
    payload['choices'][0]['due_at'] = None
    with pytest.raises(ConflictError):
        await service.confirm(task_env.who,task.task_id,OfficeActionsDecision.model_validate(payload))


async def test_unique_exact_quote_is_anchored_by_server_not_model_arithmetic(task_env):
    """模型偏移算错但完整引用唯一存在时，服务端按固定正本确定精确位置。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who,env.record)
    task = await store.transition(env.who,task.task_id,'running',expected_version=1,at=1001.0)
    service = ActionProposalService(env.workspace,store,now=lambda:1002.0)
    entries = await service.prepare(env.who,task,[{'kind':'todo','title':'整理材料','owner':'self',
        'source':{'input_name':'input_1.txt','start':0,'end':7,'quote':'minutes'}}],
        {'input_1.txt':'private minutes'})
    assert entries[0]['candidate']['source'] == {'input_name':'input_1.txt','start':8,'end':15,'quote':'minutes'}


@pytest.mark.parametrize('text,quote',[('private minutes','missing'),('minutes minutes','minutes')])
async def test_missing_or_ambiguous_quote_is_never_guessed(task_env, text, quote):
    """找不到引用或多次出现且原偏移不匹配时，不选择其中一个位置。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who,env.record)
    task = await store.transition(env.who,task.task_id,'running',expected_version=1,at=1001.0)
    service = ActionProposalService(env.workspace,store,now=lambda:1002.0)
    with pytest.raises(InvalidRequestError):
        await service.prepare(env.who,task,[{'kind':'todo','title':'整理材料','owner':'self',
            'source':{'input_name':'input_1.txt','start':1,'end':2,'quote':quote}}],{'input_1.txt':text})
