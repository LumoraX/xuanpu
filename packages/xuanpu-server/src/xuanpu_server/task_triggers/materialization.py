"""触发实例的资格与原子受理，模型调用仍只属于R2-A。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError
from xuanpu_server.quota import usage_day
from xuanpu_server.runtime.task_model_budget import text_request_bound
from xuanpu_server.work_tasks.execution_models import BudgetReservation, ExecutionGrant
from xuanpu_server.work_tasks.execution_store import initialize
from xuanpu_server.work_tasks.models import TERMINAL, InputFile, WorkTaskRecord
from xuanpu_server.work_tasks.store import save_record, validate_sources

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.service import WorkTaskService
    from xuanpu_server.workspace.models import WorkspaceState

    from .models import Occurrence, TaskDefinition


def occurrence_id(key: str) -> str:
    """稳定身份保留为去重墓碑，重试不会另造触发实例。"""
    return 'occ_'+hashlib.sha256(key.encode()).hexdigest()[:40]


def overlap(state: WorkspaceState,record: TaskDefinition) -> bool:
    """同定义尚未终结的任务均视为重叠，不能积压无限批准卡。"""
    return any(value.get('definition_id')==record.definition_id and value['status'] not in TERMINAL
               for value in state.work_tasks.values())


async def qualification(tasks: WorkTaskService,state: WorkspaceState,record: TaskDefinition,
                        candidate: WorkTaskRecord,now: float) -> str | None:
    """只判断可核定的受理额度，实际模型请求仍逐次完整预留。"""
    validate_sources(state,candidate)
    grant=ExecutionGrant.model_validate_json(json.dumps(state.task_grants[record.grant_id]))
    if grant.revoked or not grant.accepting_new_tasks or now>=grant.expires_at:
        return 'grant_revoked'
    if record.executions>=record.confirmed.max_executions or grant.attempts_used>=grant.max_attempts:
        return 'execution_limit'
    used=sum(BudgetReservation.model_validate_json(json.dumps(value)).charged for value in state.task_reservations.values()
             if value['grant_id']==record.grant_id)
    minimum=text_request_bound({})+4096
    tenant=await tasks.workspace._control.get_tenant(record.tenant_id)
    usage=await tasks.workspace._control.get_usage(record.tenant_id,usage_day(now))
    if used+minimum>grant.max_tokens or (tenant and tenant.quota.max_daily_tokens is not None
                                       and usage.total_tokens+minimum>tenant.quota.max_daily_tokens):
        return 'budget_unavailable'
    if len(state.work_tasks)>=tasks.store.max_tasks:
        return 'task_quota'
    if tasks.workspace._used_bytes(state)>=tasks.workspace.quota_bytes:
        return 'object_quota'
    return None


def accept(state: WorkspaceState,who: Principal,record: TaskDefinition,occurrence: Occurrence,
           prepared: WorkTaskRecord,now: float,origin: tuple[str,...]) -> tuple[Occurrence,WorkTaskRecord]:
    """实例、固定输入、WorkTask、dispatch同员工事务提交。"""
    key=occurrence.occurrence_id
    task=prepared.model_copy(update={'task_id':'task_'+key.removeprefix('occ_'),
        'request_id':key,'grant_id':record.grant_id,'definition_id':record.definition_id,
        'definition_version':record.version,'trigger_origin_chain':(*origin,record.definition_id),
        'accepted_at':now,'updated_at':now})
    if task.task_id in state.work_tasks:
        raise ConflictError('触发任务已存在但发生实例未结算，请核查')
    task=initialize(state,task,who)
    save_record(state,task,'accepted')
    occurrence=occurrence.model_copy(update={'status':'accepted','task_id':task.task_id,
        'input_files':tuple(InputFile(item_id=s.item_id,revision=s.revision) for s in task.inputs)})
    state.task_occurrences[key]=occurrence.model_dump(mode='json')
    return occurrence,task
