"""在现有HTTP工具闸门内消费管理员核定的员工连接。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.connections.package_bindings import binding_key
from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.tenancy import Principal, lower_role
from xuanpu_server.work_tasks.execution_context import current_execution
from xuanpu_server.work_tasks.execution_models import ExecutionGrant

if TYPE_CHECKING:
    import taifeng
    from xuanpu_spec.manifest import ConnectorSpec
    from xuanpu_spec.openapi import Operation

    from xuanpu_server.connections.broker import CredentialBroker
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.runtime.events import SubmissionIndex


class ManagedHttpTools:
    """工具参数和模型均不能决定员工主体、安装或token。"""
    def __init__(self,broker: CredentialBroker,control: ControlPlaneStore,
                 submissions: SubmissionIndex) -> None:
        """用受保护会话索引和数据库记录还原当前调用者。"""
        self.broker,self.control,self.submissions=broker,control,submissions

    async def lookup(self,tenant: str,digest: str,connector: str,operation: str) -> dict[str,Any] | None:
        """读取不可变绑定；缺失才沿用原有非托管HTTP协议。"""
        async with self.broker.service.store.transaction(tenant) as state:
            record=state.records.get(binding_key(digest,connector,operation))
            return None if record is None else dict(record)

    @staticmethod
    def validate(binding: dict[str,Any],spec: ConnectorSpec,operation: Operation,base_url: str) -> None:
        """拒绝混用静态认证、额外参数、写工具或不匹配URL。"""
        if (spec.auth is not None or base_url!='https://qyapi.weixin.qq.com'
            or operation.path!=binding['endpoint'] or operation.method.upper()!='GET'
            or spec.effect_of(operation.operation_id,read_only=operation.read_only)!='read'
            or operation.parameters or operation.request_body is not None):
            raise PermissionDeniedError('业务包与核定只读绑定不一致；不得混用静态认证或额外参数')

    async def invoke(self,tenant: str,digest: str,spec: ConnectorSpec,operation: Operation,
                     base_url: str,args: dict[str,Any],ctx: taifeng.ToolContext) -> tuple[str,bool]:
        """每次从当前会话还原员工，后台必须使用原任务固定引用。"""
        binding=await self.lookup(tenant,digest,spec.name,operation.operation_id)
        if binding is None:
            raise PermissionDeniedError('托管连接绑定不存在')
        self.validate(binding,spec,operation,base_url)
        extra=ctx.extras.get('submission_id')
        ref=self.submissions.session_of(extra if isinstance(extra,str) else None,ctx.thread_id)
        if ref.user_id is None or ref.session_id is None:
            raise PermissionDeniedError('企业只读工具需要已认证员工会话')
        session=await self.control.get_session(tenant,ref.session_id)
        if session is None or session.user_id!=ref.user_id:
            raise PermissionDeniedError('员工会话不匹配')
        user=await self.broker.service.identity.get_user(tenant,ref.user_id)
        if user is None or user.status!='active':
            raise PermissionDeniedError('员工不可用')
        who=Principal(role=user.role,tenant_id=tenant,key_id=None,user_id=user.user_id)
        if session.created_by.startswith('key:'):
            key_id=session.created_by.split('/',1)[0].removeprefix('key:')
            key=await self.control.get_api_key(key_id)
            if key is None or not key.active or not key.can_act_as_user or key.tenant_id!=tenant:
                raise PermissionDeniedError('原代理凭据已失效')
            who=Principal(role=lower_role(user.role,key.role),tenant_id=tenant,key_id=key_id,user_id=user.user_id)
        connection_ref=None
        if session.work_task_id is not None:
            execution=current_execution.get()
            if execution is None or execution.lease.task_id!=session.work_task_id:
                raise PermissionDeniedError('任务执行上下文缺失')
            # task_store读取须在协调事务外；执行授权是原员工固定授权，不提升角色。
            from xuanpu_server.work_tasks.store import require_task
            async with execution.store.workspace.transaction(who,'work-task:connection:'+session.work_task_id) as state:
                task_record=require_task(state,session.work_task_id)
                grant=ExecutionGrant.model_validate_json(json.dumps(state.task_grants[task_record.grant_id or '']))
            who=await execution.store.authorize(grant,execution.now())
            async with self.broker.service.store.transaction(tenant) as connection_state:
                connection_ref=next((r for r in task_record.connection_refs if r.installation_id==binding['installation_id']
                    and binding['operation'] in connection_state.records.get(r.consent_id,{}).get('operations',[])),None)
            if connection_ref is None or connection_ref not in grant.connection_refs:
                raise PermissionDeniedError('connection_reauthorization_required')
        result=await self.broker.read(who,binding['installation_id'],binding['operation'],args,ref=connection_ref)
        return json.dumps(result,ensure_ascii=False),False
