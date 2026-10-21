"""逐条事件租约与原子发生实例受理，乱序提交不影响发现。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)

from .materialization import occurrence_id
from .models import Delivery, DomainEvent, Occurrence
from .store import definition
from .time_service import snapshots

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal

    from .service import TaskDefinitionService


async def claim(service: TaskDefinitionService,who: Principal,identifier: str) -> tuple[Delivery,DomainEvent] | None:
    """员工锁内逐条认领，未提交的较小序号行会在以后扫描仍被发现。"""
    if not service.config.enabled or not service.tasks.durable.enabled:
        return None
    async with service.workspace.transaction(who,'work-task:trigger-event:'+identifier) as state:
        value=state.task_event_deliveries.get(identifier)
        if value is None:
            return None
        delivery=Delivery.model_validate_json(json.dumps(value))
        now=service.transaction_time(state)
        if delivery.status=='settled' or delivery.lease_until>now or delivery.next_check_at>now:
            return None
        delivery=delivery.model_copy(update={'status':'claimed','owner':service.tasks.process_owner,
            'epoch':delivery.epoch+1,'lease_until':now+60.})
        state.task_event_deliveries[identifier]=delivery.model_dump(mode='json')
        event=DomainEvent.model_validate_json(json.dumps(state.task_domain_events[delivery.event_id]))
        return delivery,event


async def deliver(service: TaskDefinitionService,who: Principal,identifier: str) -> Occurrence | None:
    """锁外读取精确源版本，再以原owner/epoch提交；过期回执不能推进。"""
    claimed=await claim(service,who,identifier)
    if claimed is None:
        return None
    lease,event=claimed
    captured=await service.get(who,lease.definition_id)
    candidate,reason=None,None
    try:
        candidate=await snapshots(service,who,captured,event.input_files)
    except (ConflictError,InvalidRequestError,NotFoundError):
        reason='source_unavailable'
    key=f'event:{lease.definition_id}:{lease.definition_version}:{event.event_id}'
    oid=occurrence_id(key)
    async with service.workspace.transaction(who,'work-task:trigger-event:'+identifier) as state:
        now=service.transaction_time(state)
        current=Delivery.model_validate_json(json.dumps(state.task_event_deliveries[identifier]))
        if current.status!='claimed' or (current.owner,current.epoch)!=(lease.owner,lease.epoch) or current.lease_until<=now:
            raise ConflictError('stale_delivery：事件投递租约失效')
        record=definition(state,lease.definition_id)
        occurrence=Occurrence(occurrence_id=oid,definition_id=record.definition_id,definition_version=lease.definition_version,
            occurrence_key=key,scheduled_at=event.occurred_at,source_event_id=event.event_id,created_at=now)
        if record.cancelled or record.version!=lease.definition_version:
            reason='definition_changed'
        elif not service.config.enabled or not service.tasks.durable.enabled:
            reason='feature_disabled'
        elif not record.enabled:
            reason='definition_paused'
        from .eligibility import evaluate

        if reason=='definition_changed':
            occurrence=occurrence.model_copy(update={'status':'cancelled','reason_code':reason})
        elif reason in {'definition_paused','feature_disabled'}:
            occurrence=occurrence.model_copy(update={'status':'blocked','reason_code':reason,
                'next_eligibility_check_at':now+service.config.eligibility_seconds})
        else:
            occurrence=await evaluate(service,state,record,occurrence,candidate,reason,event.trusted_origin_chain,now)
        reason=occurrence.reason_code
        state.task_occurrences[oid]=occurrence.model_dump(mode='json')
        target='blocked' if occurrence.status=='blocked' and reason not in {'grant_revoked','execution_limit'} else 'settled'
        state.task_event_deliveries[identifier]=current.model_copy(update={'status':target,'lease_until':0.,
            'next_check_at':now+service.config.eligibility_seconds,'occurrence_id':oid,'reason_code':reason}).model_dump(mode='json')
    await service.wake(who,occurrence)
    return occurrence
