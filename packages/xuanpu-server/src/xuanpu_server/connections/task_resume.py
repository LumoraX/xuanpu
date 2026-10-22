"""本人重新同意后显式继续原任务，预算和attempt历史不重置。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.connections.rules import fingerprint
from xuanpu_server.connections.task_bindings import snapshot
from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.execution_store import dispatch_of
from xuanpu_server.work_tasks.models import TERMINAL
from xuanpu_server.work_tasks.store import require_task, save_record

if TYPE_CHECKING:
    from xuanpu_server.connections.models import RevokeInput
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.models import WorkTaskRecord
    from xuanpu_server.work_tasks.service import WorkTaskService


async def resume(tasks: WorkTaskService,who: Principal,identifier: str,body: RevokeInput) -> WorkTaskRecord:
    """仅原员工浏览器确认有限新引用；已有工具批准不自动提交。"""
    connections=tasks.connections
    if connections is None:
        raise ConflictError('员工连接尚未配置')
    await connections.actor(who,browser=True)
    task=await tasks.store.get(who,identifier)
    if task.definition_id is not None:
        raise ConflictError('安排生成的任务须在原安排重新确认，不得修改共享执行授权')
    if not task.connection_refs:
        raise ConflictError('原任务没有员工连接授权')
    refs=await snapshot(connections,who,task.package_digest)
    if {r.installation_id for r in refs}!={r.installation_id for r in task.connection_refs}:
        raise ConflictError('原任务连接范围已变化')
    request='work-task:connection-resume:'+identifier+':'+body.request_id
    digest=fingerprint(body.model_dump())
    async with tasks.workspace.transaction(who,request) as state:
        current=require_task(state,identifier)
        old=state.requests.get(request)
        if old is not None:
            if old['fingerprint']!=digest:
                raise ConflictError('继续请求标识已用于不同内容')
            return current
        dispatch=dispatch_of(state,identifier)
        if (current.state_version!=body.expected_version or current.status in TERMINAL
            or current.resources_pending or dispatch.state=='leased'):
            raise ConflictError('任务版本已变化或仍有执行资源，请刷新进展后再确认')
        grant=state.task_grants[current.grant_id or '']
        if grant['revoked'] or grant['expires_at']<=tasks._now():
            raise ConflictError('原任务执行授权已到期，不能扩大原恢复窗口')
        state.task_grants[current.grant_id or '']={**grant,'connection_refs':[r.model_dump() for r in refs]}
        updated=current.model_copy(update={'connection_refs':refs,'state_version':current.state_version+1,
                                           'updated_at':tasks._now()})
        save_record(state,updated,'connection_reauthorized')
        state.task_dispatch[identifier]=dispatch.model_copy(update={
            'state':'waiting_user' if updated.pending else 'ready','block_reason':None,
            'due_at':tasks._now()}).model_dump(mode='json')
        state.requests[request]={'fingerprint':digest,'result':{'task_id':identifier,'state_version':updated.state_version}}
    if not updated.pending and tasks.executor is not None:
        tasks.executor.start(who,updated)
    return updated
