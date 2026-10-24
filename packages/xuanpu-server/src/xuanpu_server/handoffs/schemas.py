"""交接原生确认字段与固定成果引用，不接受身份或对象路径覆盖。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xuanpu_server.shared_matters.schemas import SharedChange, SharedMutation


class ResultRef(BaseModel):
    """交付引用固定共享版本及真实SHA，不能使用自由文本当成果。"""
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    material_id: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class SubmitHandoff(SharedChange):
    """创建先核对事项版本，再固定目标员工及明确资料包。"""
    shared_id: str = Field(min_length=1, max_length=160)
    target_kind: Literal['user', 'unit']
    target_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500, pattern=r'\S')
    summary: str = Field(min_length=1, max_length=4000, pattern=r'\S')
    include_content: bool = False
    material_revision_ids: tuple[str, ...] = Field(default=(), max_length=8)
    due_at: float | None = None
    policy_id: str | None = Field(default=None, min_length=1, max_length=160)


class ExplainHandoff(SharedChange):
    """退回、补充请求、取消请求及争议必须有本人明确说明。"""
    reason: str = Field(min_length=1, max_length=4000, pattern=r'\S')


class ProvideInformation(ExplainHandoff):
    """发起人更新精确输入并重新提交，不复用原承办或审批。"""
    shared_version: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=4000, pattern=r'\S')
    include_content: bool = False
    material_revision_ids: tuple[str, ...] = Field(default=(), max_length=8)


class TransferHandoff(ProvideInformation):
    """转交同时重新明确接收人、共享版本及本次精确输入。"""
    user_id: str = Field(min_length=1, max_length=160)


class DeliverHandoff(SharedChange):
    """只引用当前已经ready的成果，所需审批由服务器核对。"""
    result_refs: tuple[ResultRef, ...] = Field(min_length=1, max_length=8)
    approval_id: str | None = Field(default=None, min_length=1, max_length=160)


class SignOffHandoff(SharedChange):
    """签收须明确本次交付版本，不能引用过期交付或后台状态。"""
    delivery_version: int = Field(ge=1)


class ResultUpload(SharedMutation):
    """结果发布同时核对交接和共享事项，正文仍经有界上传而非主机路径。"""
    expected_version: int = Field(ge=1)
    shared_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=255)

    @model_validator(mode='after')
    def valid_name(self) -> ResultUpload:
        """沿用共享文件名边界，参数不能形成目录。"""
        if self.name in ('.', '..') or '/' in self.name or '\\' in self.name:
            raise ValueError('成果名称不能包含路径')
        return self


class ResultText(ResultUpload):
    """明确发布本人受保护助手文本，服务端取正文与来源，不允许替换。"""
    source_context_id: str = Field(min_length=1,max_length=160)
    draft_message_id: int = Field(ge=1)
