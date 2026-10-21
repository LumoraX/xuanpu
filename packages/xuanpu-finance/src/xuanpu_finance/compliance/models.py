"""F3合规策略、真实税务快照及人工命令的严格契约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from xuanpu_finance.expenses.models import ApprovalStep, Money, Reason, StrictModel, Version
from xuanpu_finance.models import Identifier  # noqa: TC001 - 运行时模型解析

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
AuthorityRevision = Annotated[int, Field(ge=1, le=9223372036854775807)]
Period = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
ComplianceAction = Literal["verify_invoice", "confirm_usage", "confirm_tax_entry", "confirm_red_letter", "archive", "audit_archive", "refresh_red_letter"]


def aware_time(value: str) -> str:
    """外部事实的时间必须包含明确时区。"""
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("时间必须含时区")
    return value


def validate_json_text(value: object) -> None:
    """数据库JSONB不能表达NUL或孤立代理字符，外部事实须提前严格拒绝。"""
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("外部事实不能包含NUL字符")
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_json_text(key)
            validate_json_text(item)
    elif isinstance(value, list):
        for item in value:
            validate_json_text(item)


class CompliancePolicy(StrictModel):
    """公司税务、归档岗位和期限默认关闭，正式标准不得隐式启用。"""

    tenant_id: Identifier
    legal_entity_id: Identifier
    version: Version
    enabled: bool = False
    tax_enabled: bool = False
    archive_enabled: bool = False
    tax_provider: Identifier | None = None
    tax_configuration_version: Version | None = None
    archive_provider: Identifier | None = None
    archive_configuration_version: Version | None = None
    tax_executor_user_ids: list[Identifier] = Field(default_factory=list)
    archive_executor_user_ids: list[Identifier] = Field(default_factory=list)
    tax_steps: list[ApprovalStep] = Field(default_factory=list)
    archive_steps: list[ApprovalStep] = Field(default_factory=list)
    open_periods: list[Period] = Field(default_factory=list)
    usage_kind: Literal["vat_deduction"] = "vat_deduction"
    supported_invoice_profiles: list[Identifier] = Field(default_factory=list)
    plan_hours: Annotated[int, Field(ge=1, le=72)] = 24
    verification_max_age_hours: Annotated[int, Field(ge=1, le=168)] = 24
    archive_profile_id: Identifier | None = None
    archive_profile_version: Version | None = None
    retention_class: Identifier = "unconfigured"
    retention_days: Annotated[int, Field(ge=1, le=36500)] = 3650
    retention_anchor: Literal["archived_at", "invoice_issued_at", "period_end"] = "archived_at"
    required_lock_mode: Literal["SIMULATION", "COMPLIANCE"] = "COMPLIANCE"
    required_legal_hold: bool = False

    @model_validator(mode="after")
    def authorities(self) -> Self:
        """岗位和审批步骤必须唯一，启用能力须显式给出完整配置。"""
        for people in (self.tax_executor_user_ids, self.archive_executor_user_ids, self.open_periods, self.supported_invoice_profiles):
            if len(people) != len(set(people)):
                raise ValueError("配置不能重复")
        for steps in (self.tax_steps, self.archive_steps):
            if len({s.id for s in steps}) != len(steps) or len({s.approver_user_id for s in steps}) != len(steps):
                raise ValueError("审批步骤和人员不能重复")
        if self.enabled and self.tax_enabled and (not self.tax_provider or not self.tax_configuration_version or not self.tax_steps or not self.tax_executor_user_ids):
            raise ValueError("税务能力缺少渠道或实际岗位")
        if self.enabled and self.archive_enabled and (not self.archive_provider or not self.archive_configuration_version or not self.archive_steps or not self.archive_executor_user_ids or not self.archive_profile_id or not self.archive_profile_version):
            raise ValueError("归档能力缺少渠道、岗位或标准profile")
        return self


class TaxRedLetterCase(StrictModel):
    """由税务权威返回的已有红字确认单，确认不代表已经开票。"""

    external_case_id: Identifier
    confirmation_id: Identifier
    authority_revision: AuthorityRevision
    status: Literal["awaiting_confirmation", "confirmed", "issued", "cancelled", "expired", "rejected"]
    our_side: bool
    initiated_at: str
    authority_deadline: str
    blue_invoice_id: Identifier
    buyer_tax_id: Identifier
    seller_tax_id: Identifier
    amount: Money
    tax: Money
    red_invoice_number: Identifier | None = None
    line_adjustments: dict[Identifier, Money] = Field(default_factory=dict)
    line_tax_adjustments: dict[Identifier, Money] = Field(default_factory=dict)

    @field_validator("initiated_at", "authority_deadline")
    @classmethod
    def aware(cls, value: str) -> str:
        """确认时限保留权威原始发起时间。"""
        return aware_time(value)

    @model_validator(mode="after")
    def positive_amount(self) -> Self:
        """红冲金额为正的冲减量，税额不能超过含税金额。"""
        if Decimal(self.amount) <= 0 or Decimal(self.tax) > Decimal(self.amount):
            raise ValueError("红冲金额或税额无效")
        if datetime.fromisoformat(self.initiated_at) > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("原确认单发起时间不能来自未来")
        if self.status == "issued" and not self.red_invoice_number:
            raise ValueError("已开红票必须有实际红票号码")
        return self


class TaxAdjustment(StrictModel):
    """税务权威的调整义务，不把材料收到解释为申报完成。"""

    external_adjustment_id: Identifier
    status: Literal["required", "submitted", "completed", "cancelled"]
    reason: Reason
    amount: Money
    tax: Money
    accounting_period: Period | None = None


class TaxSnapshot(StrictModel):
    """完整税务快照及单调版本，不允许任意回执字典替代业务事实。"""

    kind: Literal["tax"]
    authority_revision: AuthorityRevision
    observed_at: str
    valid_until: str
    verification_status: Literal["not_checked", "pending", "passed", "failed", "unavailable"]
    invoice_status: Literal["normal", "void", "abnormal", "red_locked", "fully_red"]
    usage_status: Literal["unconfirmed", "confirmed", "revoked"]
    tax_entry_status: Literal["unconfirmed", "confirmed", "revoked"]
    usage_kind: Literal["vat_deduction"] | None = None
    accounting_period: Period | None = None
    verification_id: Identifier | None = None
    usage_confirmation_id: Identifier | None = None
    tax_entry_confirmation_id: Identifier | None = None
    red_letter_cases: list[TaxRedLetterCase]
    adjustments: list[TaxAdjustment]

    @field_validator("observed_at", "valid_until")
    @classmethod
    def aware(cls, value: str) -> str:
        """所有有效期必须有时区。"""
        return aware_time(value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        """有效期和确认标识必须支持实际成功语义。"""
        validate_json_text(self.model_dump(mode="json"))
        if datetime.fromisoformat(self.valid_until) <= datetime.fromisoformat(self.observed_at):
            raise ValueError("税务快照有效期必须晚于查验时间")
        if self.verification_status == "passed" and not self.verification_id:
            raise ValueError("通过查验必须有实际权威查验标识")
        if self.usage_status == "confirmed" and (not self.usage_confirmation_id or not self.accounting_period or self.usage_kind != "vat_deduction"):
            raise ValueError("用途确认缺少实际确认标识或期间")
        if self.tax_entry_status == "confirmed" and (not self.tax_entry_confirmation_id or not self.accounting_period):
            raise ValueError("税务入账确认缺少实际确认标识或期间")
        if len({c.external_case_id for c in self.red_letter_cases}) != len(self.red_letter_cases):
            raise ValueError("税务快照确认单不能重复")
        if len({a.external_adjustment_id for a in self.adjustments}) != len(self.adjustments):
            raise ValueError("税务调整义务不能重复")
        return self


class ComplianceActionParameters(StrictModel):
    """合规动作只接收人类意图，金额及来源由持久事实推导。"""

    accounting_period: Period | None = None
    confirmation_id: Identifier | None = None
    workflow_step_id: Identifier | None = None
    reason: Reason | None = None
    evidence_ids: list[Identifier] | None = None
    parent_manifest_hash: Digest | None = None
    source_event_ids: list[Identifier] | None = None

    @field_validator("evidence_ids", "source_event_ids")
    @classmethod
    def unique_ids(cls, value: list[str] | None) -> list[str] | None:
        """材料与补包事件引用不得重复。"""
        if value is not None and len(value) != len(set(value)):
            raise ValueError("引用不能重复")
        return value


class ComplianceActionPreview(StrictModel):
    """税务、红字和归档目标独立于报销父单据。"""

    action: Literal["confirm_usage", "confirm_tax_entry", "confirm_red_letter", "archive", "audit_archive", "approve", "reject"]
    target_type: Literal["invoice", "red_letter_case", "archive", "expense_claim", "payable", "action_plan"]
    target_id: Identifier
    expected_version: Version
    parameters: ComplianceActionParameters

    @model_validator(mode="after")
    def parameter_matrix(self) -> Self:
        """动作参数与目标严格匹配，不支持发起确认单、开红票或申报。"""
        required = {"confirm_usage": {"accounting_period"}, "confirm_tax_entry": {"accounting_period"}, "confirm_red_letter": {"confirmation_id"}, "archive": set(), "audit_archive": set(), "approve": {"workflow_step_id"}, "reject": {"workflow_step_id", "reason"}}[self.action]
        allowed = required | ({"parent_manifest_hash", "source_event_ids"} if self.action == "archive" else {"reason", "evidence_ids"} if self.action == "approve" else set())
        fields = self.parameters.model_fields_set
        values = self.parameters.model_dump()
        if not required <= fields or not fields <= allowed or any(values[k] is None for k in fields):
            raise ValueError("合规动作参数缺失或不适用")
        targets = {"confirm_usage": {"invoice"}, "confirm_tax_entry": {"invoice"}, "confirm_red_letter": {"red_letter_case"}, "archive": {"invoice", "expense_claim", "payable"}, "audit_archive": {"archive"}, "approve": {"action_plan"}, "reject": {"action_plan"}}
        if self.target_type not in targets[self.action]:
            raise ValueError("合规动作目标类型不适用")
        if self.action == "archive" and ((self.parameters.parent_manifest_hash is None) != (self.parameters.source_event_ids is None)):
            raise ValueError("补充包必须同时绑定父清册和来源事件")
        return self


class RedLetterInvoiceLink(StrictModel):
    """实际人工将独立导入查验的红票关联已有权威确认单。"""

    expected_case_version: Version
    blue_invoice_id: Identifier
    expected_blue_invoice_version: Version
    red_invoice_id: Identifier
    expected_red_invoice_version: Version


class TaxAdjustmentEvidence(StrictModel):
    """人工补件只追加材料收到事实，不确认申报或抵扣。"""

    expected_version: Version
    reason: Reason
    evidence_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]

    @field_validator("evidence_ids")
    @classmethod
    def distinct(cls, value: list[str]) -> list[str]:
        """补充材料不得重复。"""
        if len(value) != len(set(value)):
            raise ValueError("材料不能重复")
        return value


class ComplianceProviderDescriptor(StrictModel):
    """无凭据持久渠道描述，业务库拒绝密钥、路径及未声明字段。"""

    tenant_id: Identifier
    legal_entity_id: Identifier
    provider: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
    configuration_version: Version
    adapter_version: Identifier
    base_url: Annotated[str, Field(min_length=1, max_length=2048)]
    kind: Literal["tax", "archive"]
    simulation: bool
    enabled: bool = True
    supports_idempotency: bool = False
    supports_query: bool = False
    supports_state_revision: bool = False
    supports_object_versions: bool = False
    supports_retention_readback: bool = False

    @model_validator(mode="after")
    def required_capabilities(self) -> Self:
        """启用描述必须具备安全恢复与对应真实事实核对能力。"""
        needed = [self.supports_idempotency, self.supports_query]
        needed.extend([self.supports_state_revision] if self.kind == "tax" else [self.supports_object_versions, self.supports_retention_readback])
        if self.enabled and not all(needed):
            raise ValueError("合规渠道缺少幂等、查询或事实读回能力")
        return self
