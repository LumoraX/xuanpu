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


class EvaluationCase(StrictModel):
    """一个评测用例：入口、输入、预定义检查（没有插件或脚本）。"""
    name: str = Field(min_length=1, max_length=80)
    entry: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=8000)
    contains: tuple[str, ...] = Field(default=(), max_length=20)
    not_contains: tuple[str, ...] = Field(default=(), max_length=20)
    max_tokens: int | None = Field(default=None, ge=1, le=100_000)


class EvaluationSuite(StrictModel):
    """评测套件（严格 JSON，最多 50 个用例）；摘要须等于交付描述声明的 ``evaluation_suite_sha256``。"""
    format: Literal['xuanpu-eval-suite/v1']
    cases: tuple[EvaluationCase, ...] = Field(min_length=1, max_length=50)


class CaseOutcome(StrictModel):
    """一个用例的实际结果与独立判定（不听模型自报）。"""
    name: str
    passed: bool
    reasons: tuple[str, ...] = ()
    outcome: str
    total_tokens: int = 0
    reply_excerpt: str = ''


class CapabilityEvaluation(Record):
    """一次隔离评测：固定能力修订、安装配置摘要、套件摘要与预算；结果逐用例保存。"""
    capability_id: str
    capability_revision: int
    installation_id: str
    config_id: str
    config_digest: str
    suite_digest: str
    suite: EvaluationSuite
    token_budget: int = Field(ge=1000, le=100_000)
    status: Literal['running', 'passed', 'failed', 'interrupted', 'blocked'] = 'running'
    reason: str | None = None
    sessions: tuple[str, ...] = ()
    cases: tuple[CaseOutcome, ...] = ()
    finished_at: float | None = None


class ReleaseTarget(StrictModel):
    """灰度目标：固定员工与其生命周期代次。"""
    user_id: str = Field(min_length=1, max_length=160)
    user_epoch: int = Field(ge=1)


class CapabilityRelease(Record):
    """一次发布：精确能力修订、安装配置、评测证据与固定员工名单（最多 100 人）。

    active 才可新受理；retired 让已绑定工作按原授权收尾；revoked 代次加一，已绑定工作也不能继续。
    """
    capability_id: str
    capability_revision: int
    installation_id: str
    config_id: str
    config_digest: str
    evaluation_id: str
    targets: tuple[ReleaseTarget, ...] = Field(min_length=1, max_length=100)
    status: Literal['active', 'retired', 'revoked'] = 'active'
    epoch: int = Field(default=1, ge=1)
    supersedes: str | None = None


class AcceptanceBatch(Record):
    """冻结的验收批次：能力修订、验收标准与样本任务；建后不可改，变更须另建新批次（supersedes）。"""
    capability_id: str
    capability_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    criteria: str = Field(min_length=1, max_length=2000)
    samples: tuple[str, ...] = Field(min_length=1, max_length=500)
    evidence_kind: Literal['synthetic', 'real_model', 'pilot'] = 'synthetic'
    supersedes: str | None = None
    created_by: str


class Observation(Record):
    """一条人工核实观察（只追加）：样本、结论与证据引用；纠正另记一条并指向被纠正的那条。"""
    batch_id: str
    task_id: str
    verdict: Literal['passed', 'failed', 'blocked']
    evidence_ref: str = Field(min_length=1, max_length=500)
    note: str = Field(default='', max_length=1000)
    observer: str
    supersedes: str | None = None


class CapabilityState(StrictModel):
    """同一协调事务内的登记、预检、配置和安装。"""
    revisions: dict[str, CapabilityRevision] = Field(default_factory=dict)
    previews: dict[str, InstallPreview] = Field(default_factory=dict)
    configurations: dict[str, InstallationConfig] = Field(default_factory=dict)
    installations: dict[str, Installation] = Field(default_factory=dict)
    assignments: dict[str, CapabilityAssignment] = Field(default_factory=dict)
    evaluations: dict[str, CapabilityEvaluation] = Field(default_factory=dict)
    releases: dict[str, CapabilityRelease] = Field(default_factory=dict)
    batches: dict[str, AcceptanceBatch] = Field(default_factory=dict)
    observations: dict[str, Observation] = Field(default_factory=dict)
