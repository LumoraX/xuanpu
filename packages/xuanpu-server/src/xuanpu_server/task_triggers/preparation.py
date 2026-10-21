"""锁外解析真实能力及输入，提交时复核固定摘要。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.ids import new_id
from xuanpu_server.work_tasks.models import CapabilityTaskInput, WorkTaskInput, WorkTaskRecord
from xuanpu_server.work_tasks.rules import input_fingerprint

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.service import WorkTaskService

    from .models import DefinitionCreate


async def prepare(tasks: WorkTaskService,who: Principal,request: DefinitionCreate,now: float) -> WorkTaskRecord:
    """不创建占位任务；冻结包及来源权限来自原受理服务。"""
    tenant,user=await tasks.workspace.require_employee(who)
    template=request.template
    body={'request_id':request.request_id,'text':template.text,'input_files':template.input_files,
          'matter_id':template.matter_id,'timezone':request.timezone}
    if template.kind=='office':
        model=WorkTaskInput.model_validate(body)
        name='office-assistant'
    else:
        if template.capability is None:
            raise InvalidRequestError('能力安排缺少已发布入口引用')
        model=CapabilityTaskInput.model_validate({**body,'capability':template.capability})
        name=template.capability.package
    package=await tasks.packages.resolve(tenant,name)
    if package.manifest.spec.policy.sandbox.network:
        raise InvalidRequestError('后台安排只允许断网脚本，外部访问必须经连接器')
    entry=template.capability.entry if template.capability else package.manifest.spec.skills.entries[0]
    if entry not in package.manifest.spec.skills.entries:
        raise InvalidRequestError('能力入口尚未发布')
    inputs=await tasks._inputs(who,model)
    mimes={source.mime_type in {'text/csv','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'} for source in inputs}
    if template.kind=='office' and len(mimes)!=1:
        raise InvalidRequestError('会议与表格材料应分开安排')
    return WorkTaskRecord(task_id=new_id('task'),tenant_id=tenant,user_id=user,actor=who.actor,
        request_id=request.request_id,input_fingerprint=input_fingerprint(model),text=template.text,
        timezone=request.timezone,matter_id=template.matter_id,inputs=inputs,package=package.record.name,
        package_version=package.record.version,package_digest=package.record.digest.removeprefix('sha256:'),
        entry=entry,execution_kind=template.kind,completion_policy='office_files' if template.kind=='office' else 'capability_receipt',
        output_kind=('table_summary' if True in mimes else 'meeting_minutes') if template.kind=='office' else 'capability_result',
        recovery_policy_version=2,process_owner=tasks.process_owner,accepted_at=now,updated_at=now)
