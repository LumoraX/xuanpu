"""源领域事务中冻结订阅清单，不接受模型或客户端发布可信事实。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.work_tasks.execution_context import current_execution
from xuanpu_server.work_tasks.models import InputFile

from .models import Delivery, DomainEvent

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


def emit(state: WorkspaceState,event_type: Any,subject: str,version: int,at: float,
         refs: tuple[InputFile,...],origin: tuple[str,...]) -> None:
    """只向源事务当时启用的定义版本建立唯一投递。"""
    subscriptions=[value for value in state.task_definitions.values() if value['enabled'] and
        value['confirmed']['trigger']['kind']=='event' and value['confirmed']['trigger']['event_type']==event_type
        and value['confirmed']['trigger'].get('subject_id') in {None,subject}]
    if not subscriptions:
        return
    identifier='event_'+hashlib.sha256(f'{event_type}:{subject}:{version}'.encode()).hexdigest()[:40]
    event=DomainEvent(event_id=identifier,event_type=event_type,subject_id=subject,
        subject_version=version,occurred_at=at,input_files=refs,trusted_origin_chain=origin)
    data=event.model_dump(mode='json')
    if len(json.dumps(data).encode())>8192:
        raise ValueError('可信事件元数据超过8KiB')
    state.task_domain_events[identifier]=data
    for record in subscriptions:
        key='delivery_'+hashlib.sha256(f"{identifier}:{record['definition_id']}:{record['version']}".encode()).hexdigest()[:40]
        delivery=Delivery(delivery_id=key,event_id=identifier,definition_id=record['definition_id'],
            definition_version=record['version'],next_check_at=at)
        state.task_event_deliveries.setdefault(key,delivery.model_dump(mode='json'))


def derive(old: WorkspaceState,state: WorkspaceState) -> None:
    """对真实领域变更产生事件；回滚与原业务共享同一提交。"""
    binding=current_execution.get()
    task=state.work_tasks.get(binding.lease.task_id) if binding is not None else None
    inherited=tuple(task.get('trigger_origin_chain',())) if task else ()
    for identifier,item in state.items.items():
        previous=old.items.get(identifier)
        if previous==item:
            continue
        if inherited and previous is None:
            item['_trigger_origin_chain']=list(inherited)
        origin=tuple(item.get('_trigger_origin_chain',()))
        if item['kind']=='file' and item['data']['current_revision']>(previous['data']['current_revision'] if previous else 0):
            revision=item['data']['current_revision']
            emit(state,'workspace.file_revision_committed',identifier,revision,item['updated_at'],
                 (InputFile(item_id=identifier,revision=revision),),origin)
        elif item['kind']=='todo' and item['data']['status']=='done' and (not previous or previous['data']['status']!='done'):
            emit(state,'workspace.todo_completed',identifier,item['version'],item['updated_at'],(),origin)
    for identifier,record in state.work_tasks.items():
        previous=old.work_tasks.get(identifier)
        if record['status']=='succeeded' and (not previous or previous['status']!='succeeded'):
            refs=tuple(InputFile(item_id=i['item_id'],revision=i['revision']) for i in record['inputs'])
            emit(state,'work_task.succeeded',identifier,record['state_version'],record['updated_at'],refs,
                 tuple(record.get('trigger_origin_chain',())))
