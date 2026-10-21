"""安排领域复用员工事务及R2-A任务账本。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.work_tasks.models import TERMINAL
from xuanpu_server.work_tasks.rules import advance
from xuanpu_server.work_tasks.store import parse_record, save_record

from .models import TaskDefinition

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


def definition(state: WorkspaceState,identifier: str) -> TaskDefinition:
    """员工快照已限定身份，跨人标识一律404。"""
    record=state.task_definitions.get(identifier)
    if record is None:
        raise NotFoundError('执行安排不存在')
    return TaskDefinition.model_validate_json(json.dumps(record))


def save_definition(state: WorkspaceState,record: TaskDefinition, *, revision: bool=False) -> None:
    """当前状态可推进，已确认版本范围另行不可变归档。"""
    state.task_definitions[record.definition_id]=record.model_dump(mode='json')
    if revision:
        state.task_definition_versions[f'{record.definition_id}:{record.version}']=record.model_dump(mode='json')


def check_enabled_capacity(state: WorkspaceState, identifier: str | None = None) -> None:
    """恢复及修改也须复核启用上限，原地更新启用记录不额外占名额。"""
    if sum(value['enabled'] for key, value in state.task_definitions.items() if key != identifier) >= 100:
        raise ConflictError('本人启用执行安排数量达到上限')


def check_version(record: TaskDefinition,expected: int) -> None:
    """管理操作同时防止旧版确认与当前状态竞争。"""
    if record.state_version!=expected:
        raise ConflictError('执行安排版本已变化，请重新读取')


def stop_tasks(state: WorkspaceState,record: TaskDefinition, *, stop_running: bool,now: float,pause: bool=False,all_versions: bool=False) -> None:
    """事务内停止未开始实例；在途只提出取消，不假称撤销下游。"""
    for value in list(state.work_tasks.values()):
        task=parse_record(value)
        if task.definition_id!=record.definition_id or (not all_versions and task.definition_version!=record.version) or task.status in TERMINAL:
            continue
        dispatch=state.task_dispatch[task.task_id]
        if dispatch['state']=='leased':
            if stop_running:
                save_record(state,task.model_copy(update={'cancel_requested':True,'state_version':task.state_version+1,
                    'updated_at':max(task.updated_at,now)}),'cancel_requested')
        elif pause and not stop_running:
            dispatch.update(state='blocked',block_reason='definition_paused')
        else:
            save_record(state,advance(task,'cancelled',expected_version=task.state_version,at=max(task.updated_at,now),
                cancel_requested=True),'definition_cancelled')
            dispatch.update(state='settled',block_reason='definition_cancelled')
            for occurrence in state.task_occurrences.values():
                if occurrence.get('task_id')==task.task_id:
                    occurrence.update(status='cancelled',reason_code='definition_changed')
    for value in state.task_occurrences.values():
        if not pause and value['definition_id']==record.definition_id and (all_versions or value['definition_version']==record.version) and value['status'] in {'pending','blocked'}:
            value.update(status='cancelled',reason_code='definition_changed')


def public(record: TaskDefinition) -> dict[str,Any]:
    """只暴露已确认范围和进展，不公开grant/key/owner或私有准备记录。"""
    result=record.model_dump(mode='json',exclude={'prepared','grant_id','hourly_fires','tenant_id','user_id'})
    return result
