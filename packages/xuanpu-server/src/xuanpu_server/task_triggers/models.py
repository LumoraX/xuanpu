"""后台安排、唯一发生实例和可信事件的严格领域模型。"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from xuanpu_server.work_tasks.models import (
    CapabilityReference,
    FrozenModel,
    Identifier,
    InputFile,
    Timestamp,
    WorkTaskRecord,
)
from xuanpu_server.workspace.models import timezone_name

EventType = Literal['workspace.file_revision_committed','workspace.todo_completed','work_task.succeeded']


class TriggerConfig(FrozenModel):
    """默认关闭；扫描只负责持久受理，不直接运行模型。"""
    enabled: bool = False
    scan_seconds: float = Field(default=5.,ge=.1,le=60.)
    eligibility_seconds: float = Field(default=60.,ge=1.,le=300.)


class TimeTrigger(FrozenModel):
    """带偏移首次时刻固定当地日历锚点。"""
    kind: Literal['time'] = 'time'
    repeat: Literal['once','daily','weekly'] = 'once'
    at: str = Field(min_length=1,max_length=64)


class EventTrigger(FrozenModel):
    """只订阅本人平台内已经鉴权的三种来源。"""
    kind: Literal['event'] = 'event'
    event_type: EventType
    subject_id: Identifier | None = None


Trigger = Annotated[TimeTrigger | EventTrigger,Field(discriminator='kind')]


class TaskTemplate(FrozenModel):
    """安排只引用本人明确枚举的文件及已发布能力。"""
    kind: Literal['office','capability'] = 'office'
    text: str = Field(min_length=1,max_length=8000,pattern=r'\S')
    input_files: tuple[InputFile,...] = Field(default=(),max_length=8)
    capability: CapabilityReference | None = None
    matter_id: Identifier | None = None

    @field_validator('input_files',mode='before')
    @classmethod
    def immutable_files(cls,value: Any) -> Any:
        """JSON数组转元组，不转换文件身份或revision类型。"""
        return tuple(value) if isinstance(value,list) else value


class DefinitionCreate(FrozenModel):
    """明确确认范围、时区、总执行次数和预算，不能由材料赋权。"""
    request_id: Identifier
    confirmed: Literal[True]
    title: str = Field(min_length=1,max_length=500,pattern=r'\S')
    template: TaskTemplate
    trigger: Trigger
    timezone: str = 'Asia/Shanghai'
    input_policy: Literal['fixed_revision','latest_at_occurrence'] = 'fixed_revision'
    misfire_policy: Literal['skip','coalesce_latest'] = 'skip'
    overlap_policy: Literal['skip'] = 'skip'
    max_executions: int = Field(default=3,ge=1,le=100)
    max_attempts: int = Field(default=3,ge=1,le=3)
    max_tokens: int = Field(default=300000,ge=1,le=15000000)
    valid_until: str = Field(min_length=1,max_length=64)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls,value: str) -> str:
        """共用真实IANA时区验证。"""
        return timezone_name(value)


class DefinitionUpdate(DefinitionCreate):
    """每次修改都重新确认，旧版尚未开始的授权失效。"""
    expected_version: int = Field(ge=1)
    stop_running: bool = False


class DefinitionAction(FrozenModel):
    """暂停默认不停止在途，取消总是提出停止请求。"""
    request_id: Identifier
    expected_version: int = Field(ge=1)
    stop_running: bool = False


class TaskDefinition(FrozenModel):
    """当前版本与独立grant，内部准备记录不向HTTP返回。"""
    definition_id: Identifier
    tenant_id: Identifier
    user_id: Identifier
    version: int = Field(ge=1)
    state_version: int = Field(default=1,ge=1)
    title: str
    enabled: bool = True
    cancelled: bool = False
    confirmed: DefinitionCreate
    prepared: WorkTaskRecord
    grant_id: Identifier
    next_fire_at: Timestamp | None = None
    next_eligibility_check_at: Timestamp | None = None
    pause_reason: str | None = None
    executions: int = 0
    hourly_fires: tuple[float,...] = ()
    created_at: Timestamp
    updated_at: Timestamp


class Occurrence(FrozenModel):
    """稳定触发键即去重墓碑，已结算记录不删除。"""
    occurrence_id: Identifier
    definition_id: Identifier
    definition_version: int = Field(ge=1)
    occurrence_key: str = Field(max_length=512)
    scheduled_at: Timestamp
    source_event_id: Identifier | None = None
    input_files: tuple[InputFile,...] = ()
    status: Literal['pending','accepted','missed','skipped_overlap','blocked','cancelled'] = 'pending'
    task_id: Identifier | None = None
    reason_code: str | None = None
    next_eligibility_check_at: Timestamp | None = None
    created_at: Timestamp


class DomainEvent(FrozenModel):
    """不含文件名、正文、目标或凭据的可信事务事实。"""
    event_id: Identifier
    event_type: EventType
    subject_id: Identifier
    subject_version: int = Field(ge=1)
    occurred_at: Timestamp
    input_files: tuple[InputFile,...] = ()
    trusted_origin_chain: tuple[str,...] = Field(default=(),max_length=16)


class Delivery(FrozenModel):
    """逐行租约及结算，不依赖全局最大事件ID。"""
    delivery_id: Identifier
    event_id: Identifier
    definition_id: Identifier
    definition_version: int
    status: Literal['pending','claimed','settled','blocked'] = 'pending'
    owner: str | None = None
    epoch: int = 0
    lease_until: Timestamp = 0.
    next_check_at: Timestamp = 0.
    occurrence_id: Identifier | None = None
    reason_code: str | None = None
