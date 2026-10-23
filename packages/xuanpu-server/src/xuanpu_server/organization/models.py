"""组织实体；供应商身份字段不包含玄圃管理员权限。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from xuanpu_server.connections.models import (
    ConnectionRef,  # noqa: TC001 —— Pydantic运行时解析嵌套模型
)
from xuanpu_server.organization.provider_snapshots import (  # noqa: TC001 —— Pydantic运行时解析嵌套模型
    ProposedChange,
    ProviderSnapshot,
)


class OrgRecord(BaseModel):
    """不可变的租户实体，变更须产生新版本并保留创建时间。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: str
    id: str = Field(min_length=1, max_length=160)
    version: int = Field(default=1, ge=1)
    created_at: float
    updated_at: float


class OrgUnit(OrgRecord):
    """部门ID永久稳定；停用后不复用。"""
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None
    status: Literal['active', 'disabled'] = 'active'


class Position(OrgRecord):
    """属于一个部门的岗位，不附带隐式审批权。"""
    unit_id: str
    name: str = Field(min_length=1, max_length=120)
    status: Literal['active', 'disabled'] = 'active'


class Membership(OrgRecord):
    """员工在部门及岗位的任职区间。"""
    user_id: str
    user_epoch: int = Field(default=1, ge=1)
    unit_id: str
    position_id: str | None = None
    valid_from: float
    valid_until: float | None = None
    status: Literal['active', 'revoked'] = 'active'
    epoch: int = Field(default=1, ge=1)

    @model_validator(mode='after')
    def validate_interval(self) -> Membership:
        """拒绝空区间和反向区间。"""
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError('任职结束时间必须晚于开始时间')
        return self


OrgRole = Literal['org_manager', 'unit_operator', 'unit_approver']


class RoleAssignment(OrgRecord):
    """组织职责与身份角色完全分离；空scope仅允许组织管理。"""
    user_id: str
    user_epoch: int = Field(default=1, ge=1)
    scope_id: str | None = None
    role: OrgRole
    status: Literal['active', 'revoked'] = 'active'
    epoch: int = Field(default=1, ge=1)

    @model_validator(mode='after')
    def validate_scope(self) -> RoleAssignment:
        """承办和审批必须明确属于哪个部门。"""
        if self.scope_id is None and self.role != 'org_manager':
            raise ValueError('承办和审批职责必须指定部门')
        return self


class FieldAuthority(OrgRecord):
    """每个组织字段仅有一个权威来源，local或具体安装ID。"""
    object_type: Literal['user', 'unit', 'membership']
    field: Literal['display_name', 'name', 'parent_id', 'membership', 'status']
    source: str = Field(min_length=1, max_length=160)


class AuthorityPreview(OrgRecord):
    """切换来源前冻结旧版本及影响规模，应用时再次核对。"""
    object_type: Literal['user', 'unit', 'membership']
    field: Literal['display_name', 'name', 'parent_id', 'membership', 'status']
    source: str
    source_version: int = 0
    previous_version: int
    previous_source: str | None
    affected_count: int
    status: Literal['pending', 'applied'] = 'pending'


class SourceMapping(OrgRecord):
    """供应商标识与本地稳定标识的显式映射。"""
    source: str
    object_type: Literal['user', 'unit', 'membership']
    external_id: str
    local_id: str


class SyncPreview(OrgRecord):
    """只读采集的完整性与来源版本；确认前不改变本地组织。"""
    source: str
    source_revision: str
    complete: bool
    status: Literal['pending', 'applied', 'stale'] = 'pending'
    snapshot: ProviderSnapshot
    connection_ref: ConnectionRef
    changes: tuple[ProposedChange, ...]
    conflicts: tuple[dict[str, JsonValue], ...] = ()
    applied_changes: tuple[str, ...] = ()


class OrganizationState(BaseModel):
    """协调事务拥有的类型化组织快照。"""
    units: dict[str, OrgUnit] = Field(default_factory=dict)
    positions: dict[str, Position] = Field(default_factory=dict)
    memberships: dict[str, Membership] = Field(default_factory=dict)
    roles: dict[str, RoleAssignment] = Field(default_factory=dict)
    authority: dict[str, FieldAuthority] = Field(default_factory=dict)
    authority_previews: dict[str, AuthorityPreview] = Field(default_factory=dict)
    mappings: dict[str, SourceMapping] = Field(default_factory=dict)
    previews: dict[str, SyncPreview] = Field(default_factory=dict)
