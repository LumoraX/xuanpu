"""可信链与有限触发次数，不能通过客户端metadata清除来源。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .store import save_definition

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState

    from .models import TaskDefinition


def pause(state: WorkspaceState,record: TaskDefinition,reason: str,now: float) -> None:
    """自动暂停与一次有意义的站内通知同事务，不默认向外发送。"""
    updated=record.model_copy(update={'enabled':False,'pause_reason':reason,
        'state_version':record.state_version+1,'updated_at':now})
    state.task_grants[record.grant_id]['accepting_new_tasks']=False
    if reason=='grant_revoked':
        state.task_grants[record.grant_id]['revoked']=True
    save_definition(state,updated)
    if reason in {'hourly_limit','chain_depth'}:
        state.pending_notifications.append({'notification_key':f'task-definition:{record.definition_id}:{record.version}:{reason}',
            'source':'task_definition','task_definition_id':record.definition_id,'title':'后台执行安排已暂停',
            'body':'触发频率或关联链超过已允许的范围，请核实后再恢复。',
            'created_at':now,'alert_after':now,'channels':['inbox']})


def guard(state: WorkspaceState,record: TaskDefinition,origin: tuple[str,...],now: float) -> str | None:
    """同定义自身链直接跳过；超过深度或每小时20次则持久暂停。"""
    if record.definition_id in origin:
        return 'same_origin_chain'
    if len(origin)>=3:
        pause(state,record,'chain_depth',now)
        return 'chain_depth'
    if sum(value>now-3600. for value in record.hourly_fires)>=20:
        pause(state,record,'hourly_limit',now)
        return 'hourly_limit'
    return None
