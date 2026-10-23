"""共享写操作要求明确确认、操作号和当前事项版本。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.organization.models import OrgRole  # noqa: TC001 —— Pydantic运行时解析枚举


class SharedMutation(BaseModel):
    """固定请求不能携带调用租户、操作员工或任意路径。"""
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    request_id: str = Field(min_length=1, max_length=160)
    confirmed: Literal[True]


class CreateMatterInput(SharedMutation):
    """明确创建共享事项，不从私人事项自动复制。"""
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(default='', max_length=20000)


class SharedChange(SharedMutation):
    """每次变更针对确切的事项版本。"""
    expected_version: int = Field(ge=1)


class ChangeMatterInput(SharedChange):
    """编辑正文、关闭及归档都不能充当业务签收。"""
    title: str | None = Field(default=None, min_length=1, max_length=500)
    content: str | None = Field(default=None, max_length=20000)
    status: Literal['active', 'closed', 'archived'] | None = None


class SharedMemberInput(SharedChange):
    """明确参与者、职责及限定材料范围；owner只能独立转交。"""
    subject_kind: Literal['user', 'unit']
    subject_id: str
    role: Literal['editor', 'viewer']
    scope: Literal['full', 'packet'] = 'full'
    packet_title: str = Field(default='', max_length=500)
    include_content: bool = False
    material_revisions: tuple[str, ...] = Field(default=(), max_length=100)
    required_org_roles: tuple[OrgRole, ...] = Field(default=(), max_length=3)
    role_scope_id: str | None = None
    expires_at: float | None = None


class TransferOwnerInput(SharedChange):
    """明确转交唯一owner，旧owner授权撤销且历史保留。"""
    user_id: str


class PublishMaterialInput(SharedChange):
    """仅发布本人明确的私人文件版本，不接受来源员工或对象路径。"""
    source_item_id: str = Field(min_length=1, max_length=160)
    source_revision: int = Field(ge=1)
    material_id: str | None = Field(default=None, min_length=1, max_length=160)
