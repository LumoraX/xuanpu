"""时间与事件受理共用授权、环路、来源和预算检查。"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.work_tasks.execution_models import ExecutionGrant
from xuanpu_server.work_tasks.execution_store import ExecutionStore

from .events import guard, pause
from .materialization import accept, overlap, qualification
from .store import save_definition

if TYPE_CHECKING:
    from xuanpu_server.work_tasks.models import WorkTaskRecord
    from xuanpu_server.workspace.models import WorkspaceState

    from .models import Occurrence, TaskDefinition
    from .service import TaskDefinitionService


async def evaluate(service: TaskDefinitionService,state: WorkspaceState,record: TaskDefinition,
                   occurrence: Occurrence,candidate: WorkTaskRecord | None,reason: str | None,
                   origin: tuple[str,...],now: float) -> Occurrence:
    """所有任务以原grant身份受理，扫描器本身不取得员工权限。"""
    if reason is None:
        reason=guard(state,record,origin,now)
    if reason=='same_origin_chain':
        return occurrence.model_copy(update={'status':'skipped_overlap','reason_code':reason})
    if reason is None and overlap(state,record):
        return occurrence.model_copy(update={'status':'skipped_overlap','reason_code':'previous_task_active'})
    grant=ExecutionGrant.model_validate_json(json.dumps(state.task_grants[record.grant_id]))
    try:
        original=await ExecutionStore(service.workspace).authorize(grant,now)
    except PermissionDeniedError:
        reason='grant_revoked'
    if reason is None and candidate is not None:
        try:
            reason=await qualification(service.tasks,state,record,candidate,now)
        except (ConflictError,InvalidRequestError,NotFoundError):
            reason='source_unavailable'
    decision=None
    if reason is None and candidate is not None and service.tasks.admission is not None:
        # 纳管能力的发生实例在实际发生时解析并固定（R4-B1）：不凭定义时准备的旧版本偷偷运行；
        # 定义固定的版本须仍是员工此刻的决策版本，否则阻塞，不换版本
        try:
            decision=await service.tasks.admission.admit(original,candidate.package,candidate.entry,
                                                         candidate.package_version)
            # 完成契约同样只由能力修订决定（R4-B2）：文件型能力的发生实例走办公成果校验
            profile=await service.tasks.capability_profile(record.tenant_id,decision,candidate.inputs)
            candidate=candidate.model_copy(update=asdict(profile))
        except (InvalidRequestError,NotFoundError,PermissionDeniedError):
            reason='capability_unavailable'
    if reason is not None:
        if reason in {'grant_revoked','execution_limit'}:
            pause(state,record,reason,now)
        return occurrence.model_copy(update={'status':'blocked','reason_code':reason,
            'next_eligibility_check_at':now+service.config.eligibility_seconds})
    assert candidate is not None
    occurrence,task=accept(state,original,record,occurrence,candidate,now,origin)
    if decision is not None:
        assert service.tasks.admission is not None
        # 任务标识由发生实例确定：事务重试再写同一绑定是幂等的
        await service.tasks.admission.bind(original,'task',task.task_id,decision)
    save_definition(state,record.model_copy(update={'executions':record.executions+1,'updated_at':now,
        'hourly_fires':tuple(value for value in record.hourly_fires if value>now-3600.)+(now,)}))
    return occurrence
