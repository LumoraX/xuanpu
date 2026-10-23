"""共享领域类型；材料、发布、来源依赖及通知结构在首个迁移中完整建立。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from xuanpu_server.organization.models import OrgRole  # noqa: TC001 —— Pydantic运行时解析枚举


class SharedMatterConfig(BaseModel):
    """默认关闭新共享；保留已有读取与撤回。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = False
    tenants: tuple[str, ...] = ()
    max_matters: int = Field(default=1000, ge=1, le=10000)
    max_material_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    max_storage_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    copy_lease_seconds: int = Field(default=30, ge=5, le=300)
    max_concurrent_copies: int = Field(default=2, ge=1, le=4)

    def allows(self, tenant: str) -> bool:
        """全局开关与租户白名单必须同时匹配。"""
        return self.enabled and tenant in self.tenants


class SharedRecord(BaseModel):
    """所有共享记录带租户、稳定标识、版本和审计时间。"""
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    tenant_id: str
    id: str = Field(min_length=1, max_length=160)
    version: int = Field(default=1, ge=1)
    created_at: float
    updated_at: float


class SharedMatter(SharedRecord):
    """唯一真人owner持有共享权属，关闭不代表交付签收。"""
    owner_id: str
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(default='', max_length=20000)
    status: Literal['active', 'closed', 'archived'] = 'active'
    access_epoch: int = Field(default=1, ge=1)


class SharedMember(SharedRecord):
    """成员只关联一个明确授权；部门本身不能成为owner。"""
    matter_id: str
    grant_id: str
    subject_kind: Literal['user', 'unit']
    subject_id: str
    role: Literal['owner', 'editor', 'viewer']
    status: Literal['active', 'revoked'] = 'active'


class SharedAccessGrant(SharedRecord):
    """读取范围同时受当前人员、组织职责、固定版本、用途和期限约束。"""
    matter_id: str
    subject_kind: Literal['user', 'unit']
    subject_id: str
    subject_epoch: int | None = Field(default=None, ge=1)
    required_org_roles: tuple[OrgRole, ...] = ()
    role_scope_id: str | None = None
    purpose: Literal['direct', 'handoff', 'approval'] = 'direct'
    scope: Literal['full', 'packet'] = 'full'
    packet_title: str = Field(default='', max_length=500)
    material_revisions: tuple[str, ...] = ()
    include_content: bool = False
    content_snapshot: str | None = None
    epoch: int = Field(default=1, ge=1)
    expires_at: float | None = None
    status: Literal['active', 'revoked'] = 'active'

    @model_validator(mode='after')
    def scope_contract(self) -> SharedAccessGrant:
        """packet不能隐式继承整事项内容，用户职责条件须指明部门。"""
        if self.scope == 'packet' and not self.packet_title.strip():
            raise ValueError('限定材料范围必须有独立显示标题')
        if self.required_org_roles and self.subject_kind == 'user' and self.role_scope_id is None:
            raise ValueError('限定用户职责时必须明确职责部门')
        if self.subject_kind == 'user' and self.subject_epoch is None:
            raise ValueError('用户授权必须绑定身份代次')
        if self.include_content and self.scope == 'packet' and self.content_snapshot is None:
            raise ValueError('packet正文必须固定为授权时的副本')
        return self


class SharedEvent(SharedRecord):
    """历史正文只经当前共享ACL读取，不复制到租户管理审计。"""
    matter_id: str
    actor_id: str
    kind: str
    data: dict[str, JsonValue] = Field(default_factory=dict)


class SharedSourceRef(BaseModel):
    """共享派生物保留固定来源，不以私有中间文件抹去约束。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    shared_id: str
    material_revision_id: str | None = None
    access_epoch: int = Field(ge=1)
    dependency_id: str | None = None


class SharedMaterial(SharedRecord):
    """材料归属于共享事项，原件删除与共享撤回分别操作。"""
    matter_id: str
    publisher_id: str
    name: str = Field(min_length=1, max_length=255)
    current_revision: int = Field(default=0, ge=0)
    status: Literal['pending', 'ready', 'failed', 'withdrawn'] = 'pending'
    result_for_handoff: str | None = None


class MaterialRevision(SharedRecord):
    """已发布字节、SHA和来源永不被新文件覆盖。"""
    matter_id: str
    material_id: str
    revision: int = Field(ge=1)
    publisher_id: str
    object_key: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    size: int = Field(ge=0, le=20 * 1024 * 1024)
    mime_type: str
    source_kind: Literal['private_file', 'upload', 'text', 'shared_result']
    source_item_id: str | None = None
    source_user_id: str | None = None
    source_revision: int | None = None
    source_refs: tuple[SharedSourceRef, ...] = ()
    status: Literal['ready', 'withdrawn'] = 'ready'


class Publication(SharedRecord):
    """固定意图先提交，复制在事务外进行，完成后再核对权限与源版本。"""
    matter_id: str
    material_id: str
    publisher_id: str
    publisher_epoch: int
    expected_matter_version: int
    access_epoch: int
    revision: int = Field(ge=1)
    object_key: str
    name: str
    mime_type: str
    source_kind: Literal['private_file', 'upload', 'text', 'shared_result']
    source_item_id: str | None = None
    source_revision: int | None = None
    source_item_version: int | None = None
    source_refs: tuple[SharedSourceRef, ...] = ()
    expected_sha256: str
    expected_size: int = Field(ge=0, le=20 * 1024 * 1024)
    status: Literal['pending', 'ready', 'failed', 'withdrawn'] = 'pending'
    claim_token: str | None = None
    copy_generation: str | None = None
    copy_closed: bool = True
    uncertain_copy: bool = False
    lease_until: float = 0
    error_code: str | None = None
    object_cleaned: bool = False
    result_for_handoff: str | None = None


class SharedContext(SharedRecord):
    """真实员工拥有的受保护上下文；撤权后不复用旧引擎或缓存。"""
    user_id: str
    user_epoch: int
    kind: Literal['runtime_session', 'assistant_conversation']
    runtime_session_id: str | None = None
    status: Literal['active', 'blocked', 'closed'] = 'active'
    block_reason: str | None = None
    message_floor: int = 0


class SourceDependency(SharedRecord):
    """读取前持久保存来源证明，它本身不授予永久读取权。"""
    user_id: str
    context_id: str
    matter_id: str
    material_revision_id: str | None = None
    grant_id: str
    grant_epoch: int
    access_epoch: int
    workspace_message_id: int | None = None


class SharedNotification(SharedRecord):
    """站内动态仅存来源引用，补读和数量都重新检查当前ACL。"""
    user_id: str
    matter_id: str
    event_id: str
    semantic_key: str
    read_at: float | None = None


class SharedState(BaseModel):
    """协调事务持有的共享快照，仓储不能独立提交。"""
    matters: dict[str, SharedMatter] = Field(default_factory=dict)
    members: dict[str, SharedMember] = Field(default_factory=dict)
    grants: dict[str, SharedAccessGrant] = Field(default_factory=dict)
    history: dict[str, SharedEvent] = Field(default_factory=dict)
    materials: dict[str, SharedMaterial] = Field(default_factory=dict)
    revisions: dict[str, MaterialRevision] = Field(default_factory=dict)
    publications: dict[str, Publication] = Field(default_factory=dict)
    contexts: dict[str, SharedContext] = Field(default_factory=dict)
    dependencies: dict[str, SourceDependency] = Field(default_factory=dict)
    notifications: dict[str, SharedNotification] = Field(default_factory=dict)
