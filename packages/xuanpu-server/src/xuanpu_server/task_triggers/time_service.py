"""锁外读取正本，锁内复核资格与推进日历游标。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from xuanpu_server.work_tasks.models import InputFile, WorkTaskInput, WorkTaskRecord

from .calendar import time_occurrence_key, time_plan
from .materialization import occurrence_id
from .models import Occurrence, TaskDefinition
from .store import definition, save_definition

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal

    from .service import TaskDefinitionService


async def snapshots(service: TaskDefinitionService,who: Principal,record: TaskDefinition,
                    event_refs: tuple[InputFile,...] | None=None) -> WorkTaskRecord:
    """能力固定到原版本，最新策略只可读取已确认文件ID。"""
    package=await service.tasks.packages.resolve(record.tenant_id,record.prepared.package,record.prepared.package_version)
    if package.record.digest.removeprefix('sha256:')!=record.prepared.package_digest:
        raise ConflictError('固定能力摘要不一致')
    refs=[]
    event_versions={ref.item_id:ref.revision for ref in event_refs or ()}
    for source in record.prepared.inputs:
        revision=source.revision
        if record.confirmed.input_policy=='latest_at_occurrence':
            item=await service.workspace.get(who,source.item_id)
            revision=event_versions.get(source.item_id,item['data']['current_revision'])
        refs.append(InputFile(item_id=source.item_id,revision=revision))
    if not refs:
        return record.prepared
    request=WorkTaskInput(request_id=record.definition_id,text=record.prepared.text,input_files=tuple(refs),
                          matter_id=record.prepared.matter_id,timezone=record.prepared.timezone)
    inputs=await service.tasks._inputs(who,request)
    return record.prepared.model_copy(update={'inputs':inputs})


async def fire_time(service: TaskDefinitionService,who: Principal,identifier: str) -> Occurrence | None:
    """数据库异常全部回滚；明确资格不足单独提交blocked而不推进原时刻。"""
    if not service.config.allows(who.tenant_id) or not service.tasks.durable.allows(who.tenant_id):
        return None
    captured=await service.get(who,identifier)
    now=service.now()
    if not captured.enabled or captured.confirmed.trigger.kind!='time' or captured.next_fire_at is None or captured.next_fire_at>now:
        return None
    due,next_fire,status=time_plan(captured.next_fire_at,captured.confirmed.timezone,
                                  captured.confirmed.trigger.repeat,now,captured.confirmed.misfire_policy)
    key=time_occurrence_key(identifier,captured.version,datetime.fromtimestamp(due,UTC))
    oid=occurrence_id(key)
    candidate,reason=None,None
    try:
        candidate=await snapshots(service,who,captured)
    except (ConflictError,InvalidRequestError,NotFoundError):
        reason='source_unavailable'
    async with service.workspace.transaction(who,'work-task:trigger-fire:'+oid) as state:
        current=definition(state,identifier)
        if (not service.config.allows(who.tenant_id) or not service.tasks.durable.allows(who.tenant_id) or not current.enabled or current.version!=captured.version
                or current.next_fire_at!=captured.next_fire_at):
            return None
        old=state.task_occurrences.get(oid)
        if old and old['status'] not in {'pending','blocked'}:
            return Occurrence.model_validate_json(json.dumps(old))
        occurrence=Occurrence(occurrence_id=oid,definition_id=identifier,definition_version=current.version,
            occurrence_key=key,scheduled_at=due,created_at=now)
        if status=='missed':
            occurrence=occurrence.model_copy(update={'status':'missed','reason_code':'misfire_window_expired'})
        else:
            from .eligibility import evaluate

            if candidate is not None and current.confirmed.input_policy=='latest_at_occurrence' and any(
                    state.items.get(s.item_id,{}).get('data',{}).get('current_revision')!=s.revision for s in candidate.inputs):
                raise ConflictError('固定输入在受理前发生变化，请重试')
            occurrence=await evaluate(service,state,current,occurrence,candidate,reason,(),now)
        current=definition(state,identifier)
        state.task_occurrences[oid]=occurrence.model_dump(mode='json')
        blocked=occurrence.status=='blocked'
        current=current.model_copy(update={'next_fire_at':current.next_fire_at if blocked else next_fire,
            'next_eligibility_check_at':occurrence.next_eligibility_check_at,
            'updated_at':now})
        save_definition(state,current)
    await service.wake(who,occurrence)
    return occurrence
