"""工作任务的严格输入、不可变来源与持久业务状态。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.connections.models import (
    ConnectionRef,  # noqa: TC001 —— Pydantic运行时解析持久引用
)
from xuanpu_server.workspace.models import timezone_name

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Timestamp = Annotated[float, Field(ge=0, le=253402300799, allow_inf_nan=False)]
Status = Literal["accepted", "running", "waiting_user", "succeeded", "failed", "cancelled",
                 "outcome_unknown", "interrupted"]
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "outcome_unknown", "interrupted"})


class FrozenModel(BaseModel):
    """拒绝额外字段和隐式类型转换，字段在验证后不再赋值。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class InputFile(FrozenModel):
    """员工只选择本人文件与明确版本。"""

    item_id: Identifier
    revision: int = Field(ge=1)


class WorkTaskInput(FrozenModel):
    """交办目标，不含可由客户端选择的身份、包、执行器或预算。"""

    request_id: Identifier
    text: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    matter_id: Identifier | None = None
    input_files: tuple[InputFile, ...] = Field(min_length=1, max_length=8)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)

    @field_validator("input_files", mode="before")
    @classmethod
    def immutable_files(cls, value: Any) -> Any:
        """JSON数组转为不可变元组，不转换元素的身份与版本类型。"""
        return tuple(value) if isinstance(value, list) else value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        """采用与原生工作区相同的真实时区校验。"""
        return timezone_name(value)


class CapabilityReference(FrozenModel):
    """租户已发布目录的入口引用，不接收任意可执行内容。"""

    package: Identifier
    entry: Identifier


class CapabilityTaskInput(WorkTaskInput):
    """通用能力允许无附件；预算、执行身份和版本仍由服务器冻结。"""

    capability: CapabilityReference
    input_files: tuple[InputFile, ...] = Field(default=(), max_length=8)


class InputSnapshot(InputFile):
    """服务端固定的输入来源，不含对象键和宿主路径。"""

    sha256: Digest
    size: int = Field(ge=1, le=20 * 1024 * 1024)
    mime_type: str = Field(min_length=1, max_length=127)
    relative_name: str = Field(pattern=r"^input_[1-8]\.[a-z0-9]{1,8}$")


class ArtifactIntent(FrozenModel):
    """冻结核验后、上传前保存的服务端清单，仅用于恢复已有索引。"""

    name: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=64)
    sha256: Digest
    size: int = Field(ge=1, le=20 * 1024 * 1024)
    mime_type: str = Field(min_length=1, max_length=127)


class StagedArtifact(ArtifactIntent):
    """私有对象只依据意图回收，不向员工或执行器暴露对象键。"""

    object_key: str = Field(min_length=1, max_length=512)
    attempt_no: int = Field(ge=1)
    created_at: Timestamp
    verified: bool = False
    deleted: bool = False


class ArtifactReceipt(FrozenModel):
    """已校验并保存到本人空间的成果，工具自报清单不是回执。"""

    name: str = Field(default="成果文件", min_length=1, max_length=255)
    role: str = Field(default="result", min_length=1, max_length=64)

    item_id: Identifier
    revision: int = Field(ge=1)
    sha256: Digest
    size: int = Field(ge=1, le=20 * 1024 * 1024)
    mime_type: str = Field(min_length=1, max_length=127)
    input_refs: tuple[InputFile, ...]
    generated_at: Timestamp


class WorkTaskRecord(FrozenModel):
    """服务端身份、固定能力版本与带版本号的完整任务记录。"""

    task_id: Identifier
    tenant_id: Identifier
    user_id: Identifier
    actor: str = Field(min_length=1, max_length=300)
    request_id: Identifier
    input_fingerprint: Digest
    matter_id: Identifier | None = None
    text: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    timezone: str
    inputs: tuple[InputSnapshot, ...]
    package: Identifier
    package_version: Identifier
    package_digest: Digest
    recovery_policy_version: Literal[1, 2] = 1
    grant_id: Identifier | None = None
    connection_refs: tuple[ConnectionRef,...] = ()
    definition_id: Identifier | None = None
    definition_version: int | None = Field(default=None, ge=1)
    trigger_origin_chain: tuple[str, ...] = Field(default=(),max_length=16)
    execution_kind: Literal['office', 'capability'] = 'office'
    entry: Identifier | None = None
    completion_policy: Literal['office_files', 'capability_receipt'] = 'office_files'
    attempt_no: int = Field(default=0, ge=0)
    result_summary: str = Field(default='', max_length=16000)
    result_receipt: dict[str, Any] = Field(default_factory=dict, max_length=256)
    output_kind: Literal["meeting_minutes", "table_summary", "capability_result"] = "meeting_minutes"
    output_hashes: dict[str, str] = Field(default_factory=dict, max_length=8)
    error_message: str | None = Field(default=None, max_length=1024)
    session_id: Identifier | None = None
    status: Status = "accepted"
    state_version: int = Field(default=1, ge=1)
    process_owner: Identifier
    accepted_at: Timestamp
    updated_at: Timestamp
    finished_at: Timestamp | None = None
    error_code: str | None = Field(default=None, max_length=128)
    artifacts: tuple[ArtifactReceipt, ...] = ()
    artifact_intents: tuple[ArtifactIntent, ...] = Field(default=(), max_length=8)
    staging_objects: tuple[StagedArtifact, ...] = Field(default=(), max_length=24)
    frozen_staging: tuple[str, ...] = Field(default=(), max_length=8)
    unsafe_resume: bool = False
    resume_evidence: tuple[dict[str, Any], ...] = Field(default=(), max_length=128)
    artifact_recovery_pending: bool = False
    proposal_ids: tuple[Identifier, ...] = ()
    outputs_verified: bool = False
    decisions_complete: bool = True
    contains_external_input: Literal[True] = True
    cancel_requested: bool = False
    resources_pending: bool = False
    resources_recovered: bool = False
    unverified_write: bool = False
    unverified_calls: tuple[str, ...] = Field(default=(), max_length=256)
    pending_decision: dict[str, Any] = Field(default_factory=dict, max_length=16)
    decision_fingerprints: dict[str, str] = Field(default_factory=dict, max_length=256)
    pending: tuple[dict[str, Any], ...] = Field(default=(), max_length=256)
    action_candidates: tuple[dict[str, Any], ...] = Field(default=(), max_length=32)
    action_results: tuple[dict[str, Any], ...] = Field(default=(), max_length=32)


class WorkTaskEvent(FrozenModel):
    """只读持久进展；不携带工具原始参数或内部异常栈。"""

    event_id: int = Field(ge=1)
    task_id: Identifier
    state_version: int = Field(ge=1)
    at: Timestamp
    event_kind: str
    status: Status
    error_code: str | None = None
    execution_stage: str | None = None
    attempt_no: int = 0
    next_run_at: Timestamp | None = None
    block_reason: str | None = None
    recovering: bool = False
    recovery_mode: str | None = None
    source_occurrence: dict[str, Any] | None = None


def needs_recovery(value: dict[str, Any], dispatch: dict[str, Any] | None = None) -> bool:
    """启动时也修复旧终态派发索引；正常执行仍只认领非终态。"""
    return (value['status'] not in TERMINAL or value.get('resources_pending', False)
        or value.get('artifact_recovery_pending', False) or (
            value.get('error_code') == 'container_cleanup_unverified' and not value.get('resources_recovered', False))
        or (value.get('recovery_policy_version',1)==2 and dispatch is not None and dispatch['state']!='settled'))
