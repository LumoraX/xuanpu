"""F2受控目录、执行岗位和固定审批策略。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from xuanpu_finance.expenses.models import ApprovalStep, BusinessRef, Money, StrictModel, Version
from xuanpu_finance.models import Identifier  # noqa: TC001 - Pydantic运行时解析


class BusinessSnapshot(StrictModel):
    """实际取得的采购资料快照，不能由聊天声明为已核实。"""

    tenant_id: Identifier
    legal_entity_id: Identifier
    ref: BusinessRef
    name: Annotated[str, Field(min_length=1, max_length=500)]
    verification_state: Literal["not_checked", "verified", "unavailable"]
    allowed_user_ids: Annotated[list[Identifier], Field(min_length=1)]
    amount: Money | None = None
    party_tax_id: Annotated[str, Field(max_length=64)] | None = None
    counterparty_tax_id: Annotated[str, Field(max_length=64)] | None = None
    occurred_on: str | None = None

    @model_validator(mode="after")
    def business_kind(self) -> Self:
        """采购目录不接收账户引用。"""
        if self.ref.kind not in {"supplier", "contract", "order", "acceptance"}:
            raise ValueError("业务资料类型无效")
        return self


class PayeeAccount(StrictModel):
    """已核实账户的渠道令牌及精确主体归属。"""

    tenant_id: Identifier
    legal_entity_id: Identifier
    ref: BusinessRef
    subject_id: Identifier
    account_name: Annotated[str, Field(min_length=1, max_length=256)]
    masked_account: Annotated[str, Field(min_length=1, max_length=64)]
    provider_account_id: Identifier
    verified: bool
    allowed_user_ids: Annotated[list[Identifier], Field(min_length=1)]

    @model_validator(mode="after")
    def account_kind(self) -> Self:
        """收款账户引用必须明确是员工或供应商账户。"""
        if self.ref.kind not in {"employee_account", "supplier_account"}:
            raise ValueError("账户类型无效")
        return self


class SettlementPolicy(StrictModel):
    """公司分别配置资金、记账经办和专用实际人工审批链。"""

    tenant_id: Identifier
    legal_entity_id: Identifier
    version: Version
    enabled: bool
    payment_provider: Identifier
    payment_configuration_version: Version
    posting_provider: Identifier
    posting_configuration_version: Version
    ledger_id: Identifier
    open_periods: Annotated[list[str], Field(max_length=120)]
    payment_executor_user_ids: Annotated[list[Identifier], Field(min_length=1)]
    posting_executor_user_ids: Annotated[list[Identifier], Field(min_length=1)]
    payment_steps: Annotated[list[ApprovalStep], Field(min_length=1)]
    posting_steps: Annotated[list[ApprovalStep], Field(min_length=1)]
    reversal_steps: Annotated[list[ApprovalStep], Field(min_length=1)]
    plan_hours: Annotated[int, Field(ge=1, le=72)] = 24
    require_verified_invoice: bool = True

    @model_validator(mode="after")
    def distinct_authorities(self) -> Self:
        """付款与记账岗位分离，每条链不得重复步骤或人员。"""
        import re

        if set(self.payment_executor_user_ids) & set(self.posting_executor_user_ids):
            raise ValueError("付款与记账经办岗位不能交叉")
        for people in (self.payment_executor_user_ids, self.posting_executor_user_ids):
            if len(people) != len(set(people)):
                raise ValueError("经办人员不能重复")
        for steps in (self.payment_steps, self.posting_steps, self.reversal_steps):
            if len({step.id for step in steps}) != len(steps) or len(
                {step.approver_user_id for step in steps}
            ) != len(steps):
                raise ValueError("专用审批步骤和人员不能重复")
        if len(self.open_periods) != len(set(self.open_periods)) or any(
            not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period) for period in self.open_periods
        ):
            raise ValueError("开放期间无效或重复")
        return self
