"""本人确认、版本化管理和撤销执行安排。"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError, InvalidRequestError
from xuanpu_server.ids import new_id
from xuanpu_server.work_tasks.execution_models import ExecutionGrant
from xuanpu_server.work_tasks.store import validate_sources
from xuanpu_server.workspace.rules import audit, remember, replay

from .calendar import parse_time, recurring_first
from .models import (
    DefinitionAction,
    DefinitionCreate,
    DefinitionUpdate,
    TaskDefinition,
    TriggerConfig,
)
from .preparation import prepare
from .store import check_enabled_capacity, check_version, definition, save_definition, stop_tasks

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.service import WorkTaskService
    from xuanpu_server.workspace.models import WorkspaceState


class TaskDefinitionService:
    """只有已确认范围可创建授权，提醒和模型建议不能自动启用。"""

    def __init__(self,tasks: WorkTaskService, *, now: Callable[[],float],config: TriggerConfig) -> None:
        """同一工作区、任务执行器和注入时钟。"""
        self.tasks,self.workspace,self.now,self.config=tasks,tasks.workspace,now,config

    def transaction_time(self, state: WorkspaceState) -> float:
        """生产PG复用事务时钟，显式注入时钟时保留确定性验收。"""
        return state.transaction_at if self.now is time.time and state.transaction_at is not None else self.now()

    async def preview(self, who: Principal, body: dict[str, Any]) -> dict[str, Any]:
        """只读核对范围，内部候选grant不保存，返回仍需明确确认的请求。"""
        await self.workspace.require_employee(who)
        if body.get('confirmed') is not False:
            raise InvalidRequestError('执行预览不能代替本人确认')
        self._require_enabled(who)
        request=DefinitionCreate.model_validate({**body,'confirmed':True})
        prepared=await prepare(self.tasks,who,request,self.now())
        record,grant=self._new(who,request,prepared)
        files=[]
        for source in prepared.inputs:
            item=await self.workspace.get(who,source.item_id)
            files.append({'name':item['data']['name'],'item_id':source.item_id,'revision':source.revision})
        subject_name=None
        if request.trigger.kind=='event' and request.trigger.subject_id:
            if request.trigger.event_type=='work_task.succeeded':
                subject_name=(await self.tasks.store.get(who,request.trigger.subject_id)).text
            else:
                subject=await self.workspace.get(who,request.trigger.subject_id)
                expected='file' if request.trigger.event_type=='workspace.file_revision_committed' else 'todo'
                if subject['kind']!=expected or subject.get('trashed_at'):
                    raise InvalidRequestError('事件来源类型无效或已移入回收站')
                subject_name=subject['data'].get('name') or subject['data'].get('title')
        package=await self.tasks.packages.resolve(prepared.tenant_id,prepared.package,prepared.package_version)
        draft=request.model_dump(mode='json')
        draft['confirmed']=False
        return {'request':draft,'display':{'files':files,'capability':package.manifest.metadata.description,
            'next_fire_at':record.next_fire_at,'expires_at':grant.expires_at,'event_subject':subject_name,'task_token_limit':self.tasks.durable.max_task_tokens}}

    async def create(self,who: Principal,body: DefinitionCreate | dict[str,Any]) -> TaskDefinition:
        """定义、独立grant与幂等回执原子提交，没有伪造任务。"""
        request=body if isinstance(body,DefinitionCreate) else DefinitionCreate.model_validate(body)
        async with self.workspace.transaction(who,'work-task:trigger-request:'+request.request_id) as state:
            old=replay(state,'trigger:'+request.request_id,request.model_dump(mode='json'))
            if old is not None:
                return TaskDefinition.model_validate_json(json.dumps(old))
        self._require_enabled(who)
        prepared=await prepare(self.tasks,who,request,self.now())
        record,grant=self._new(who,request,prepared)
        async with self.workspace.transaction(who,'work-task:trigger-request:'+request.request_id) as state:
            old=replay(state,'trigger:'+request.request_id,request.model_dump(mode='json'))
            if old is not None:
                return TaskDefinition.model_validate_json(json.dumps(old))
            self._require_enabled(who)
            check_enabled_capacity(state)
            if len(state.task_definitions)>=1000:
                raise ConflictError('本人执行安排数量达到上限')
            validate_sources(state,prepared)
            state.task_grants[grant.grant_id]=grant.model_dump(mode='json')
            save_definition(state,record,revision=True)
            remember(state,'trigger:'+request.request_id,request.model_dump(mode='json'),record.model_dump(mode='json'))
            audit(state,who.actor,'task_definition_confirmed',self.now(),{'id':record.definition_id,'kind':'task_definition'})
        return record

    def _require_enabled(self,who: Principal) -> None:
        """关闭开关仍可查询暂停取消，但不能产生新授权。"""
        if not self.config.allows(who.tenant_id) or not self.tasks.durable.allows(who.tenant_id):
            raise ConflictError('后台触发安排尚未启用')

    def _new(self,who: Principal,request: DefinitionCreate,prepared: Any) -> tuple[TaskDefinition,ExecutionGrant]:
        """冻结明确有效期和有限总次数，单任务重试另受R2-A上限。"""
        now=self.now()
        deadline=parse_time(request.valid_until,request.timezone)
        next_fire=None
        if request.trigger.kind=='time':
            parser=parse_time if request.trigger.repeat=='once' else recurring_first
            next_fire=parser(request.trigger.at,request.timezone)
        if deadline<=now or (next_fire is not None and not now<next_fire<=deadline):
            raise InvalidRequestError('执行时刻须在未来且不晚于授权截止时间')
        identifier,grant_id=new_id('definition'),new_id('grant')
        grant=ExecutionGrant(grant_id=grant_id,tenant_id=prepared.tenant_id,user_id=prepared.user_id,actor=who.actor,
            role='tenant_admin' if who.role=='tenant_admin' else 'tenant_member',key_id=who.key_id,
            connection_refs=prepared.connection_refs,
            package_digest=prepared.package_digest,input_digests=tuple(i.sha256 for i in prepared.inputs),
            input_file_ids=tuple(i.item_id for i in prepared.inputs),input_policy=request.input_policy,
            created_at=now,expires_at=deadline,task_attempt_limit=request.max_attempts,max_attempts=request.max_attempts*request.max_executions,max_tokens=request.max_tokens)
        record=TaskDefinition(definition_id=identifier,tenant_id=prepared.tenant_id,user_id=prepared.user_id,
            version=1,title=request.title,confirmed=request,prepared=prepared,grant_id=grant_id,
            next_fire_at=next_fire,created_at=now,updated_at=now)
        return record,grant

    async def get(self,who: Principal,identifier: str) -> TaskDefinition:
        """关闭开关或任务终结不影响本人补读。"""
        async with self.workspace.transaction(who,'work-task:trigger-read:'+identifier) as state:
            return definition(state,identifier)

    async def list(self,who: Principal, *, after_id: str | None=None,limit: int=100) -> list[TaskDefinition]:
        """稳定本人分页，管理角色不扩大内容权限。"""
        if type(limit) is not int or not 1<=limit<=100:
            raise InvalidRequestError('分页数量无效')
        async with self.workspace.transaction(who,'work-task:trigger-list') as state:
            return [definition(state,key) for key in sorted(state.task_definitions) if after_id is None or key>after_id][:limit]

    async def update(self,who: Principal,identifier: str,body: DefinitionUpdate | dict[str,Any]) -> TaskDefinition:
        """修改重新确认新grant，取消旧版未开始实例，在途按明确参数收尾。"""
        request=body if isinstance(body,DefinitionUpdate) else DefinitionUpdate.model_validate(body)
        key='trigger:'+request.request_id
        payload={'definition_id':identifier,**request.model_dump(mode='json')}
        async with self.workspace.transaction(who,'work-task:trigger-request:'+request.request_id) as state:
            prior=replay(state,key,payload)
            if prior is not None:
                return TaskDefinition.model_validate_json(json.dumps(prior))
        self._require_enabled(who)
        base=DefinitionCreate.model_validate(request.model_dump(exclude={'expected_version','stop_running'}))
        prepared=await prepare(self.tasks,who,base,self.now())
        fresh,grant=self._new(who,base,prepared)
        async with self.workspace.transaction(who,'work-task:trigger-request:'+request.request_id) as state:
            old=replay(state,key,payload)
            if old is not None:
                return TaskDefinition.model_validate_json(json.dumps(old))
            self._require_enabled(who)
            current=definition(state,identifier)
            check_version(current,request.expected_version)
            if current.cancelled:
                raise ConflictError('已取消安排不能修改')
            check_enabled_capacity(state,identifier)
            validate_sources(state,prepared)
            stop_tasks(state,current,stop_running=request.stop_running,now=self.now())
            state.task_grants[current.grant_id]['accepting_new_tasks']=False
            state.task_grants[grant.grant_id]=grant.model_dump(mode='json')
            fresh=fresh.model_copy(update={'definition_id':identifier,'version':current.version+1,
                'state_version':current.state_version+1,'created_at':current.created_at})
            save_definition(state,fresh,revision=True)
            remember(state,key,payload,fresh.model_dump(mode='json'))
        return fresh

    async def act(self,who: Principal,identifier: str,action: str,body: DefinitionAction | dict[str,Any]) -> TaskDefinition:
        """暂停不隐含取消在途；取消不宣称撤销已经发出的外部操作。"""
        request=body if isinstance(body,DefinitionAction) else DefinitionAction.model_validate(body)
        if action not in {'pause','resume','cancel'}:
            raise InvalidRequestError('安排操作无效')
        payload={'action':action,'definition_id':identifier,**request.model_dump(mode='json')}
        async with self.workspace.transaction(who,'work-task:trigger-request:'+request.request_id) as state:
            key='trigger:'+request.request_id
            old=replay(state,key,payload)
            if old is not None:
                return TaskDefinition.model_validate_json(json.dumps(old))
            current=definition(state,identifier)
            check_version(current,request.expected_version)
            if current.cancelled:
                raise ConflictError('已取消安排不能恢复')
            enabled=action=='resume'
            if enabled:
                self._require_enabled(who)
                self._resume(state,current)
            else:
                stop_tasks(state,current,stop_running=request.stop_running or action=='cancel',now=self.now(),pause=action=='pause',all_versions=True)
            updated=current.model_copy(update={'enabled':enabled,'cancelled':action=='cancel',
                'state_version':current.state_version+1,'updated_at':self.now(),
                'pause_reason':None if enabled else action})
            state.task_grants[current.grant_id]['accepting_new_tasks']=enabled
            if action=='cancel':
                for revision in state.task_definition_versions.values():
                    if revision['definition_id']==identifier:
                        state.task_grants[revision['grant_id']]['revoked']=True
                        state.task_grants[revision['grant_id']]['accepting_new_tasks']=False
            save_definition(state,updated)
            remember(state,key,payload,updated.model_dump(mode='json'))
        return updated

    def _resume(self,state: WorkspaceState,record: TaskDefinition) -> None:
        """恢复原版本等待实例，暂停期间历史由原显式错过策略最多处理一次。"""
        check_enabled_capacity(state,record.definition_id)
        if state.task_grants[record.grant_id]['revoked'] or self.now()>=state.task_grants[record.grant_id]['expires_at']:
            raise ConflictError('原授权已撤销或过期，请重新确认')
        for identifier,dispatch in state.task_dispatch.items():
            task=state.work_tasks.get(identifier)
            if task and task.get('definition_id')==record.definition_id and dispatch.get('block_reason')=='definition_paused':
                dispatch.update(state='waiting_user' if task['status']=='waiting_user' else 'ready',block_reason=None,due_at=self.now())


    async def fire_time(self,who: Principal,identifier: str) -> Any:
        """受理和游标推进放在同一事务，返回真实发生记录。"""
        from .time_service import fire_time

        return await fire_time(self,who,identifier)

    async def wake(self,who: Principal,occurrence: Any) -> None:
        """提交后唤醒原唯一执行器，关闭竞态下遗漏唤醒由持久派发补领。"""
        from xuanpu_server.errors import RuntimeUnavailableError
        from xuanpu_server.work_tasks.execution_store import ExecutionStore

        if occurrence.task_id is None or self.tasks.executor is None:
            return
        task=await self.tasks.store.get(who,occurrence.task_id)
        assert task.grant_id is not None
        grant=await ExecutionStore(self.workspace).grant(who,task.grant_id)
        try:
            self.tasks.executor.start(grant.principal(),task)
        except RuntimeUnavailableError:
            # accepted/dispatch已经落库，禁止为重试再建任务。
            return

    async def occurrences(self,who: Principal,identifier: str, *, after_id: str | None=None,limit: int=100) -> builtins.list[Any]:
        """本人分页补读，墓碑不会因为压缩诊断而消失。"""
        from .models import Occurrence

        await self.get(who,identifier)
        if type(limit) is not int or not 1<=limit<=100:
            raise InvalidRequestError('发生记录分页数量无效')
        tenant,user=await self.workspace.require_employee(who)
        values=await self.workspace.store.trigger_occurrences(tenant,user,identifier,after=after_id,limit=limit)
        return [Occurrence.model_validate_json(json.dumps(value)) for value in values]

    async def revoke_unavailable(self,tenant: str,user: str) -> None:
        """内部撤销停用身份的未开始授权，不开放正文读取权限。"""
        from xuanpu_server.work_tasks.store import _lifecycle_guard

        async with self.workspace.store.transaction(tenant,user,_lifecycle_guard,'work-task:trigger-revoke',allow_inactive=True) as state:
            for value in list(state.task_definitions.values()):
                record=TaskDefinition.model_validate_json(json.dumps(value))
                if not record.cancelled:
                    stop_tasks(state,record,stop_running=True,now=self.now(),all_versions=True)
                    for revision in state.task_definition_versions.values():
                        if revision['definition_id']==record.definition_id:
                            grant=state.task_grants[revision['grant_id']]
                            grant.update(revoked=True,accepting_new_tasks=False)
                    save_definition(state,record.model_copy(update={'enabled':False,'pause_reason':'grant_revoked',
                        'state_version':record.state_version+1,'updated_at':self.now()}))


    async def deliver_event(self,who: Principal,identifier: str) -> Any:
        """可信源只有服务端投递记录，公开HTTP不提供发布事件接口。"""
        from .event_service import deliver

        return await deliver(self,who,identifier)


    async def on_user_disabled(self,tenant_id: str,user_id: str) -> None:
        """身份停用即撤销全部版本未开始授权，并保留资源清理责任。"""
        await self.revoke_unavailable(tenant_id,user_id)
