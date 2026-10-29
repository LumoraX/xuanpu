"""专用资金的严格草稿、凭据声明和显式审批制度。"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self, get_args

from pydantic import Field, model_validator

from xuanpu_finance.expenses.models import (
    ApprovalStep,
    BusinessRef,
    ExpenseLine,
    Money,
    PayableDraft,
    Reason,
    StrictModel,
    Version,
)
from xuanpu_finance.models import Identifier  # noqa: TC001

FundingScenario = Literal['prepayment', 'employee_advance', 'entrusted_collection',
                          'prepayment_clearance', 'prepayment_return']
SCENARIOS = get_args(FundingScenario)
CLOSING = frozenset({'prepayment_clearance', 'prepayment_return'})
INVOICE_SCENARIOS = frozenset({'employee_advance', 'entrusted_collection', 'prepayment_clearance'})


class FundingAuthoritySpec(StrictModel):
    """对原件事实的申报不能携带核实结果、所有者或审批状态。"""
    kind: Literal['prepayment_terms', 'employee_payment', 'collection_mandate', 'return_evidence']
    reference: Annotated[str, Field(min_length=1, max_length=256)]
    amount_limit: Money
    valid_from: str | None = None
    valid_until: str | None = None
    occurred_on: str | None = None
    account_ref: BusinessRef | None = None
    evidence_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]

    @model_validator(mode='after')
    def authority_fields(self) -> Self:
        """每类事实严格限定日期、账户和原件，不接收多余场景字段。"""
        if Decimal(self.amount_limit) <= 0 or len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError('原凭据金额必须为正且证据唯一')
        required = {
            'prepayment_terms': set(),
            'employee_payment': {'occurred_on', 'account_ref'},
            'collection_mandate': {'valid_from', 'valid_until', 'account_ref'},
            'return_evidence': {'occurred_on'},
        }[self.kind]
        values = {'valid_from': self.valid_from, 'valid_until': self.valid_until,
                  'occurred_on': self.occurred_on, 'account_ref': self.account_ref}
        if {key for key, value in values.items() if value is not None} != required:
            raise ValueError('原凭据场景字段缺失或不适用')
        for value in (self.valid_from, self.valid_until, self.occurred_on):
            if value is not None:
                ExpenseLine.canonical_date(value)
        if self.valid_from is not None and self.valid_until is not None and self.valid_from > self.valid_until:
            raise ValueError('授权起止日期倒置')
        if self.account_ref is not None:
            kind = 'employee_account' if self.kind == 'employee_payment' else 'supplier_account'
            if self.account_ref.kind != kind:
                raise ValueError('原凭据账户类型不符')
        return self


class FundingDraft(StrictModel):
    """只保存业务申报，身份、环境、额度和核实来源由领域生成。"""
    legal_entity_id: Identifier
    scenario: FundingScenario
    reason: Reason
    amount: Money
    due_date: str
    supplier_ref: BusinessRef
    business_refs: Annotated[list[BusinessRef], Field(min_length=1, max_length=100)]
    evidence_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]
    source_case_id: Identifier | None = None
    authority: FundingAuthoritySpec | None = None

    @model_validator(mode='after')
    def scenario_fields(self) -> Self:
        """合同必填，只有预付收尾允许父单，核销不接受授权声明。"""
        PayableDraft(**self.model_dump(exclude={'scenario', 'source_case_id', 'authority'}))
        if not any(ref.kind == 'contract' for ref in self.business_refs):
            raise ValueError('资金业务必须有受控合同')
        if (self.source_case_id is not None) != (self.scenario in CLOSING):
            raise ValueError('原预付引用缺失或不适用')
        kinds = {'prepayment': 'prepayment_terms', 'employee_advance': 'employee_payment',
                 'entrusted_collection': 'collection_mandate', 'prepayment_return': 'return_evidence'}
        expected = kinds.get(self.scenario)
        if (self.authority.kind if self.authority is not None else None) != expected:
            raise ValueError('场景原凭据声明缺失或类型不符')
        if self.authority is not None and not set(self.authority.evidence_ids) <= set(self.evidence_ids):
            raise ValueError('原凭据必须属于单据实际材料')
        return self


class FundingUpdate(StrictModel):
    """完整草稿修订受乐观版本约束。"""
    expected_version: Version
    draft: FundingDraft


class FundingPolicy(StrictModel):
    """五种场景分别明确配置当前实际人工顺序审批。"""
    tenant_id: Identifier
    legal_entity_id: Identifier
    version: Version
    enabled: bool
    steps: dict[FundingScenario, list[ApprovalStep]]

    @model_validator(mode='after')
    def explicit_chains(self) -> Self:
        """禁止省略场景、空链及重复步骤人员。"""
        if set(self.steps) != set(SCENARIOS):
            raise ValueError('必须逐场景显式配置审批链')
        for steps in self.steps.values():
            if not steps or len({s.id for s in steps}) != len(steps) or len({s.approver_user_id for s in steps}) != len(steps):
                raise ValueError('场景审批链为空或存在重复')
        return self
