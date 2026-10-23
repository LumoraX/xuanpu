"""原生组织操作的严格请求契约。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.organization.models import OrgRole  # noqa: TC001 —— Pydantic运行时解析角色枚举


class MutationInput(BaseModel):
    """所有新写使用稳定操作号，拒绝调用方传租户和审计字段。"""
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    request_id: str = Field(min_length=1, max_length=160)

class UnitInput(MutationInput):
    """新部门只有显示名称及可选上级。"""
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None

class PositionInput(MutationInput):
    """岗位不隐式授予审批或组织管理权限。"""
    unit_id: str
    name: str = Field(min_length=1, max_length=120)

class MembershipInput(MutationInput):
    """明确员工和部门，不按显示名猜测身份。"""
    user_id: str
    unit_id: str
    position_id: str | None = None
    valid_from: float | None = None
    valid_until: float | None = None

class RoleInput(MutationInput):
    """组织业务职责与员工身份角色分离。"""
    user_id: str
    scope_id: str | None = None
    role: OrgRole

class OrgChangeInput(MutationInput):
    """仅允许指定集合可变字段；服务层再按实体校验。"""
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: str | None = None
    position_id: str | None = None
    valid_until: float | None = None
    status: Literal['active', 'disabled', 'revoked'] | None = None


class AuthorityInput(MutationInput):
    """字段来源不能包含管理员角色或隐式审批权限。"""
    object_type: Literal['user', 'unit', 'membership']
    field: Literal['display_name', 'name', 'parent_id', 'membership', 'status']
    source: str = Field(min_length=1, max_length=160)


class ConfirmInput(MutationInput):
    """只能确认已展示的那个预览版本。"""
    expected_version: int = Field(ge=1)


class ImportInput(MutationInput):
    """只引用已核定的安装，不接受URL、Secret或任意供应商响应。"""
    connection_id: str = Field(min_length=1, max_length=160)


class ChangeSelection(BaseModel):
    """只能选中预览中的固定变更，不能向服务器注入字段。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    change_id: str
    action: str = Field(min_length=1, max_length=30)


class ImportApplyInput(ConfirmInput):
    """明确选择本批变更；每批最多100条，不默认全量。"""
    source_revision: str
    selected_changes: tuple[ChangeSelection, ...] = Field(min_length=1, max_length=100)
