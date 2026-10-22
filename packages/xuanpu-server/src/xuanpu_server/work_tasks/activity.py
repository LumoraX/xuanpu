"""任务动态及语义通知在原员工事务提交，不新增通知队列。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import Field, field_validator

from xuanpu_server.proactive.rules import alert_time

from .models import TERMINAL, FrozenModel, WorkTaskEvent, WorkTaskRecord
from .views import execution_fields

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


class ActivityConfig(FrozenModel):
    """默认关闭，灰度只作用于明确列出的租户；空名单表示显式全量启用。"""
    enabled: bool = False
    tenant_ids: tuple[str,...] = Field(default=(),max_length=1000)

    @field_validator('tenant_ids', mode='before')
    @classmethod
    def immutable_tenants(cls, value: Any) -> Any:
        """YAML列表转不可变范围，不把员工请求当配置。"""
        return tuple(value) if isinstance(value,list) else value

    def allows(self, tenant: str) -> bool:
        """不通过模型或员工请求扩大灰度范围。"""
        return self.enabled and (not self.tenant_ids or tenant in self.tenant_ids)


def semantic(task: WorkTaskRecord, previous: dict[str,Any], dispatch: dict[str,Any]) -> str | None:
    """只取等待决定、终态、预算授权及未知结果的稳定语义身份。"""
    if task.status in TERMINAL:
        return 'terminal' if task.status!='cancelled' else None
    reason=dispatch.get('block_reason')
    if isinstance(reason,str) and any(word in reason for word in ('budget','grant','source_unavailable','block_resume','block_unknown','connection_reauthorization_required')):
        return 'blocked:'+reason
    if task.status!='waiting_user':return None
    ids={p.get('request_id',p.get('action_id','')) for p in (*task.pending,*task.action_candidates)}
    old_ids={p.get('request_id',p.get('action_id','')) for p in (*previous.get('pending',[]),*previous.get('action_candidates',[]))}
    if previous.get('status')=='waiting_user' and ids.issubset(old_ids):return None
    return 'waiting:'+hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()[:24]


def notification(task: WorkTaskRecord,key: str,at: float,channels: list[str],alert_after: float) -> dict[str,Any]:
    """通知不携带模型正文、原始参数、文件名或内部错误。"""
    titles={'succeeded':'工作已完成','failed':'工作处理未完成','interrupted':'工作已中断',
            'outcome_unknown':'工作结果待核实','waiting_user':'工作需要你的决定'}
    return {'notification_key':f'work-task:{task.task_id}:{key}','source':'work_task','work_task_id':task.task_id,
        'title':titles.get(task.status,'工作暂时无法继续'),
        'body':'请查看工作进展及成果；结果待核实时请勿重复发起同一操作。',
        'created_at':at,'alert_after':alert_after,'channels':channels}


def derive(old: WorkspaceState,state: WorkspaceState, *, enabled: bool) -> None:
    """阶段事件与原记录共用提交，轮询及重复恢复不产生新通知。"""
    new_events=state.work_task_events[len(old.work_task_events):]
    for identifier,value in state.work_tasks.items():
        previous=old.work_tasks.get(identifier,{})
        dispatch=state.task_dispatch.get(identifier,{})
        before_dispatch=old.task_dispatch.get(identifier,{})
        if previous==value and dispatch==before_dispatch:continue
        task=WorkTaskRecord.model_validate_json(json.dumps(value))
        fields=execution_fields(task,dispatch)
        own=[event for event in new_events if event['task_id']==identifier]
        for existing_event in own:existing_event.update(fields)
        if task.recovery_policy_version==2 and not own and execution_fields(task,before_dispatch)!=fields:
            task=task.model_copy(update={'state_version':task.state_version+1,
                'updated_at':max(task.updated_at,dispatch.get('changed_at',0.))})
            state.work_tasks[identifier]=task.model_dump(mode='json')
            phase_event=WorkTaskEvent(event_id=max((int(e['event_id']) for e in state.work_task_events),default=0)+1,
                task_id=identifier,state_version=task.state_version,at=task.updated_at,event_kind='execution_changed',
                status=task.status,**fields)
            state.work_task_events.append(phase_event.model_dump(mode='json'))
        key=semantic(task,previous,dispatch) if enabled else None
        if key is not None and (previous.get('status') not in TERMINAL or not previous):
            channels=list(state.settings.get("proactive_channels",["inbox"]))
            state.pending_notifications.append(notification(task,key,task.updated_at,channels,alert_time(state.settings,task.updated_at)))


def current(record: dict[str,Any] | None, dispatch: dict[str,Any], key: str | None) -> bool:
    """发送或浮层前复核语义来源，已过时等待不能继续打扰。"""
    if record is None:return False
    task=WorkTaskRecord.model_validate_json(json.dumps(record))
    meaning=semantic(task,{},dispatch)
    return meaning is not None and key==f'work-task:{task.task_id}:{meaning}'
