"""供应商目录快照只含核定字段与采集证据，不含凭据。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ProviderMember(BaseModel):
    """供应商身份保持独立，不按同名合并。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    userid: str
    name: str | None = None
    department: list[int] = Field(default_factory=list)

class ProviderDepartment(BaseModel):
    """明确成员页是否实际观察到，空列表不等于权威无人。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: int
    name: str | None = None
    parentid: int | None = None
    members: list[ProviderMember] = Field(default_factory=list)
    members_observed: bool = True

class ProviderSnapshot(BaseModel):
    """应用可见范围永不声称企业全量，保存每页摘要与错误。"""
    model_config = ConfigDict(extra='ignore', frozen=True)
    departments: list[ProviderDepartment]
    source_revision: str
    scope: Literal['application_visible']
    complete: Literal[False]
    completeness_reason: str
    pages: list[dict[str, JsonValue]]
    errors: list[dict[str, JsonValue]]
    capture_started_at: float
    capture_finished_at: float

class ProposedChange(BaseModel):
    """预览后固定的变更，不接受客户端替换实际字段或主体。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str
    object_type: Literal['unit', 'membership', 'user']
    action: Literal['create', 'update']
    external_id: str
    local_id: str
    expected_version: int
    summary: str = ''
    authority_versions: dict[str, int]
    data: dict[str, JsonValue]
