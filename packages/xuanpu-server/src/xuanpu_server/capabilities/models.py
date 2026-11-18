"""登记、预检、安装与到人分配（R4-B）；分配只授权使用能力，不授予资料、连接或审批权。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .delivery import Delivery, StrictModel


class Record(StrictModel):
    """全域记录必须携带租户、固定ID及版本。"""
    tenant_id: str
    id: str
    version: int = Field(default=1, ge=1)
    created_at: float


class CapabilityRevision(Record):
    """同一能力的每份修订只追加，描述不可覆盖。"""
    capability_id: str
    capability_key: str
    revision: int = Field(ge=1)
    digest: str
    delivery: Delivery


class InstallPreview(Record):
    """预检中的秘密变化标记只存内部，公开视图必须按白名单构造。"""
    capability_id: str
    revision: int
    expected_version: int
    expires_at: float
    public_config: dict[str, JsonValue]
    secret_refs: dict[str, str]
    connection_refs: dict[str, str]
    evidence: dict[str, JsonValue]
    evidence_digest: str
    requirements: tuple[str, ...]
    diff: dict[str, JsonValue]


class InstallationConfig(Record):
    """每次安装保存不可变配置快照，不修改此前配置。"""
    capability_id: str
    revision: int
    config_revision: int
    public_config: dict[str, JsonValue]
    secret_refs: dict[str, str]
    connection_refs: dict[str, str]
    evidence: dict[str, JsonValue]
    digest: str


class Installation(Record):
    """ready由R4-C发布事务确立，A阶段只允许staged或停止状态。"""
    capability_id: str
    config_id: str
    config_revision: int
    status: Literal['staged', 'ready', 'draining', 'disabled'] = 'staged'


class UserSelector(StrictModel):
    """直接到人：固定员工生命周期代次，员工停用或重建后旧分配不复活。"""
    kind: Literal['user']
    id: str = Field(min_length=1, max_length=160)
    user_epoch: int = Field(ge=1)


class GroupSelector(StrictModel):
    """可选按岗位或部门：经员工当前真实任职解析；目标结构版本变化须重新确认。"""
    kind: Literal['position', 'unit']
    id: str = Field(min_length=1, max_length=160)
    target_version: int = Field(ge=1)


Selector = Annotated[UserSelector | GroupSelector, Field(discriminator='kind')]


class CapabilityAssignment(Record):
    """一项分配：目标、明确的能力修订范围、有效期；撤销后代次加一且不能复活。"""
    capability_id: str
    selector: Selector
    allowed_revisions: tuple[int, ...] = Field(min_length=1, max_length=20)
    valid_until: float | None = None
    status: Literal['active', 'revoked'] = 'active'
    epoch: int = Field(default=1, ge=1)

    @model_validator(mode='after')
    def _revisions(self) -> CapabilityAssignment:
        """修订号为正且不重复：没有通配符，新增修订须重新确认权限。"""
        if any(r < 1 for r in self.allowed_revisions) or len(set(self.allowed_revisions)) != len(self.allowed_revisions):
            raise ValueError('allowed_revisions 须为不重复的正整数')
        return self


class CapabilityState(StrictModel):
    """同一协调事务内的登记、预检、配置和安装。"""
    revisions: dict[str, CapabilityRevision] = Field(default_factory=dict)
    previews: dict[str, InstallPreview] = Field(default_factory=dict)
    configurations: dict[str, InstallationConfig] = Field(default_factory=dict)
    installations: dict[str, Installation] = Field(default_factory=dict)
    assignments: dict[str, CapabilityAssignment] = Field(default_factory=dict)
