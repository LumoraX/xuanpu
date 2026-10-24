"""审批步骤由管理员预配，申请只能引用策略及固定目标。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xuanpu_server.handoffs.schemas import ResultRef  # noqa: TC001 —— Pydantic运行时嵌套模型
from xuanpu_server.shared_matters.schemas import SharedChange, SharedMutation


class StepSelector(BaseModel):
    """指定员工或部门职责互斥，不能以模型或不明确的人群作为审批人。"""
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    selector: Literal['user', 'unit_role']
    user_id: str | None = Field(default=None, min_length=1, max_length=160)
    unit_id: str | None = Field(default=None, min_length=1, max_length=160)
    role: Literal['unit_approver'] | None = None

    @model_validator(mode='after')
    def exclusive(self) -> StepSelector:
        """每步只有一种可解释的授权来源。"""
        if self.selector == 'user':
            if self.user_id is None or self.unit_id is not None or self.role is not None:
                raise ValueError('指定员工审批只能配置员工编号')
        elif self.user_id is not None or self.unit_id is None or self.role is None:
            raise ValueError('部门审批需要明确部门及审批职责')
        return self


class PolicyInput(SharedMutation):
    """首版支持一到三步顺序单人，不允许空策略。"""
    name: str = Field(min_length=1, max_length=500, pattern=r'\S')
    steps: tuple[StepSelector, ...] = Field(min_length=1, max_length=3)


class ChangePolicy(PolicyInput):
    """修改规则增加版本，历史修订继续作为旧决定的可追溯依据。"""
    expected_version: int = Field(ge=1)
    status: Literal['active', 'disabled'] = 'active'


class SubmitApproval(SharedMutation):
    """审批针对交接版本和确切成果，审批人由服务器解析。"""
    policy_id: str = Field(min_length=1, max_length=160)
    target_type: Literal['handoff']
    target_id: str = Field(min_length=1, max_length=160)
    target_version: int = Field(ge=1)
    result_refs: tuple[ResultRef, ...] = Field(min_length=1, max_length=8)


class ApprovalDecisionInput(SharedChange):
    """原生组织决定固定请求版本，理由不能被模型自动写入。"""
    reason: str = Field(default='', max_length=4000)
