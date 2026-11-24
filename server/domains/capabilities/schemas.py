"""能力HTTP输入只接受显式模板和CAS，不接受客户端执行身份。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictInt, StringConstraints, field_validator

from .delivery import Delivery, Identifier, Name, StrictModel
from .models import EvaluationSuite, ReleaseTarget, Selector  # noqa: TC001 —— Pydantic 运行时解析


class RegisterInput(StrictModel):
    """登记只读交付说明，不包含激活选项。"""
    request_id: Identifier
    delivery: Delivery


class PreviewInput(StrictModel):
    """配置键必须在模板内声明，secret_refs只接受保管库名称。"""
    request_id: Identifier
    revision: StrictInt = Field(ge=1)
    public_config: dict[Identifier, JsonValue] = Field(default_factory=dict, max_length=30)
    secret_refs: dict[Name, Name] = Field(default_factory=dict, max_length=30)
    connection_refs: dict[Name, Identifier] = Field(default_factory=dict, max_length=20)


class InstallInput(StrictModel):
    """明确确认精确预检和先前安装版本。"""
    request_id: Identifier
    preview_id: Identifier
    expected_version: StrictInt = Field(ge=0)
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_true(cls, value: object) -> object:
        """数字1不能被Literal布尔转换当成本人确认。"""
        if value is not True:
            raise ValueError('必须明确确认')
        return value


class StateInput(StrictModel):
    """ready保留为受门禁动作，A阶段永不放行。"""
    request_id: Identifier
    expected_version: StrictInt = Field(ge=1)
    status: Literal['ready', 'draining', 'disabled']


class AssignInput(StrictModel):
    """分配：目标（直接到人或岗位/部门）、1–20 项明确能力修订、可选到期时间。"""
    request_id: Identifier
    selector: Selector
    allowed_revisions: list[StrictInt] = Field(min_length=1, max_length=20)
    valid_until: float | None = Field(default=None, gt=0)


class RevokeInput(StrictModel):
    """撤销须带当前版本（CAS）。"""
    request_id: Identifier
    expected_version: StrictInt = Field(ge=1)


class EvaluateInput(StrictModel):
    """发起一次隔离评测：精确安装、套件（摘要须等于交付描述）与总 token 预算。"""
    request_id: Identifier
    capability_id: Identifier
    installation_id: Identifier
    suite: EvaluationSuite
    token_budget: StrictInt = Field(default=100_000, ge=1000, le=100_000)


class ReleasePreviewInput(StrictModel):
    """发布预览：安装、评测证据与固定员工名单（1–100 人，含代次）。"""
    installation_id: Identifier
    evaluation_id: Identifier
    targets: list[ReleaseTarget] = Field(min_length=1, max_length=100)


class ReleaseInput(ReleasePreviewInput):
    """确认发布：带预览摘要与要停止的重叠发布，明确确认。"""
    request_id: Identifier
    preview_digest: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]
    supersedes: list[Identifier] = Field(default_factory=list, max_length=100)
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_true(cls, value: object) -> object:
        """必须是 true 本身，数字 1 不算确认。"""
        if value is not True:
            raise ValueError('必须明确确认')
        return value


class ReleaseStateInput(StrictModel):
    """停止或撤销发布须带当前版本（CAS）。"""
    request_id: Identifier
    expected_version: StrictInt = Field(ge=1)
