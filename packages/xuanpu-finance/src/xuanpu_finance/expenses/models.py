"""报销命令、策略与金额的严格契约。"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xuanpu_finance.models import Identifier  # noqa: TC001 - Pydantic运行时解析字段

MAX_MONEY = Decimal("9999999999999999.99")
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,15})\.[0-9]{2}$")]
Version = Annotated[int, Field(ge=1)]
Reason = Annotated[str, Field(min_length=1, max_length=2000)]


class StrictModel(BaseModel):
    """拒绝额外字段及隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExpenseLine(StrictModel):
    """本人申报的一条费用事实，不包含审批身份。"""

    category_id: Identifier
    amount: Money
    occurred_on: str
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    department_id: Identifier | None = None
    project_id: Identifier | None = None

    @field_validator("amount")
    @classmethod
    def positive(cls, value: str) -> str:
        """每条费用必须严格大于零。"""
        if Decimal(value) <= 0:
            raise ValueError("费用必须大于零")
        return value

    @field_validator("occurred_on")
    @classmethod
    def canonical_date(cls, value: str) -> str:
        """只接收完整规范日历日期。"""
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("日期必须为YYYY-MM-DD")
        date.fromisoformat(value)
        return value


class ExpenseDraft(StrictModel):
    """公司不可在后续修订中更换的报销草稿。"""

    legal_entity_id: Identifier
    reason: Reason
    lines: Annotated[list[ExpenseLine], Field(min_length=1, max_length=100)]
    evidence_ids: Annotated[list[Identifier], Field(max_length=100)] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounded_total(self) -> Self:
        """服务端求和时拒绝numeric(18,2)溢出。"""
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("证据不能重复")
        if sum((Decimal(line.amount) for line in self.lines), Decimal(0)) > MAX_MONEY:
            raise ValueError("报销合计超出金额上限")
        return self


class ExpenseUpdate(StrictModel):
    """乐观版本约束的完整草稿修订。"""

    expected_version: Version
    draft: ExpenseDraft


class AllocationRequest(StrictModel):
    """提议分配不等于占额，提交时仍需原子复核。"""

    invoice_id: Identifier
    invoice_version: Version
    invoice_line_id: Identifier | None = None
    document_id: Identifier
    document_kind: Literal["expense_claim"]
    expected_document_version: Version
    amount: Money
    tax: Money

    @model_validator(mode="after")
    def tax_bounds(self) -> Self:
        """费用分配必须为正，税额不能超过分配金额。"""
        if Decimal(self.amount) <= 0 or Decimal(self.tax) > Decimal(self.amount):
            raise ValueError("分配金额或税额无效")
        return self


class ActionParameters(StrictModel):
    """四种F1动作专属参数，不接受身份或结果。"""

    workflow_step_id: Identifier | None = None
    reason: Reason | None = None
    evidence_ids: Annotated[list[Identifier], Field(max_length=100)] | None = None

    @field_validator("evidence_ids")
    @classmethod
    def distinct(cls, value: list[str] | None) -> list[str] | None:
        """证据引用必须唯一。"""
        if value is not None and len(value) != len(set(value)):
            raise ValueError("证据不能重复")
        return value


class ActionPreview(StrictModel):
    """预检只准备不可变计划，不产生审批事实。"""

    action: Literal["submit_claim", "approve", "reject", "withdraw"]
    target_type: Literal["expense_claim"]
    target_id: Identifier
    expected_version: Version
    parameters: ActionParameters

    @model_validator(mode="after")
    def parameters_for_action(self) -> Self:
        """严格执行动作参数矩阵。"""
        fields = self.parameters.model_fields_set
        required = {
            "submit_claim": set(),
            "approve": {"workflow_step_id"},
            "reject": {"workflow_step_id", "reason"},
            "withdraw": {"reason"},
        }[self.action]
        allowed = required | (
            {"reason", "evidence_ids"}
            if self.action == "approve"
            else {"evidence_ids"}
            if self.action == "reject"
            else set()
        )
        values = self.parameters.model_dump()
        if (
            not required <= fields
            or not fields <= allowed
            or any(values[key] is None for key in fields)
        ):
            raise ValueError("动作参数缺失或不适用")
        return self


class ActionExecution(StrictModel):
    """执行只允许引用原计划及其完整摘要。"""

    plan_id: Identifier
    plan_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ExceptionResolution(StrictModel):
    """异常处置必须绑定版本、原因及真实证据。"""

    expected_version: Version
    disposition: Literal["attach_evidence", "accept_warning", "resolve"]
    reason: Reason
    evidence_ids: Annotated[list[Identifier], Field(max_length=100)]

    @field_validator("evidence_ids")
    @classmethod
    def distinct(cls, value: list[str]) -> list[str]:
        """异常证据不允许重复引用。"""
        if len(value) != len(set(value)):
            raise ValueError("证据不能重复")
        return value


class ExpenseCategory(StrictModel):
    """配置明确允许的费用分类。"""

    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=256)]


class CostCenter(ExpenseCategory):
    """归属目录及实际可使用的员工范围。"""

    kind: Literal["department", "project"]
    allowed_user_ids: list[Identifier]


class ApprovalStep(ExpenseCategory):
    """按单据总额筛选的一步固定审批人。"""

    approver_user_id: Identifier
    minimum_amount: Money = "0.00"


class ExpensePolicy(StrictModel):
    """公司报销制度；相同版本正文不可覆盖。"""

    tenant_id: Identifier
    legal_entity_id: Identifier
    version: Version
    enabled: bool
    record_mode: Literal["invoice_backed", "internal_record"] = "invoice_backed"
    approval_due_hours: Annotated[int, Field(ge=1, le=8760)] = 72
    categories: list[ExpenseCategory]
    cost_centers: list[CostCenter]
    steps: list[ApprovalStep]
    exception_owner_user_id: Identifier
    require_verified_invoice: bool = True
    require_cost_center: bool = False
    max_claim_amount: Money = "9999999999999999.99"

    @model_validator(mode="after")
    def unique_catalog_and_people(self) -> Self:
        """禁止重复分类、归属、步骤和审批人，启用制度必须有审批链。"""
        if self.record_mode == "internal_record" and self.require_verified_invoice:
            raise ValueError("内部记录模式必须显式关闭发票查验要求")
        for entries in (self.categories, self.cost_centers, self.steps):
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError("目录标识不能重复")
        if len({step.approver_user_id for step in self.steps}) != len(self.steps):
            raise ValueError("审批人不能重复")
        if self.enabled and not self.steps:
            raise ValueError("启用制度必须有审批步骤")
        return self
