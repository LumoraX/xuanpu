"""通用合规通道的固定版本配置及严格回执信封。"""
from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xuanpu_finance.settlement.provider_models import ProviderConfig, ProviderEvidence


class ComplianceProviderConfig(ProviderConfig):
    """税务与档案通道显式声明实际可用的核对能力。"""

    kind: Literal["tax", "archive"]
    supports_state_revision: bool = False
    supports_object_versions: bool = False
    supports_retention_readback: bool = False

    @model_validator(mode="after")
    def capabilities(self) -> Self:
        """缺少安全恢复能力的配置不能启用正式或模拟动作。"""
        if self.supported_funding_scenarios:
            raise ValueError("合规通道不能声明付款记账专属资金场景能力")
        required: tuple[bool, ...] = (self.supports_idempotency, self.supports_query)
        if self.kind == "tax":
            required += (self.supports_state_revision,)
        else:
            required += (self.supports_object_versions, self.supports_retention_readback)
        if self.enabled and not all(required):
            raise ValueError("合规通道缺少必需的幂等、查单或事实读回能力")
        return self


class ComplianceTarget(BaseModel):
    """F3目标使用自己的受控状态版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=128)
    type: Literal["invoice", "red_letter_case", "archive", "expense_claim", "payable", "funding_case"]
    version: int = Field(ge=1)


class ComplianceResult(BaseModel):
    """所有结果必须属于原始固定请求，成功另验实际事实。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    protocol_version: Literal["1"]
    tenant_id: str
    legal_entity_id: str
    operation_id: str
    action: Literal["verify_invoice", "confirm_usage", "confirm_tax_entry", "confirm_red_letter",
                    "refresh_red_letter", "archive", "audit_archive"]
    external_request_id: str
    provider: str
    adapter_version: str
    configuration_version: int = Field(ge=1)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    simulation: bool
    amount: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,15})\.[0-9]{2}$")
    currency: Literal["CNY"]
    target: ComplianceTarget
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["accepted", "pending", "succeeded", "failed", "unknown", "not_found"]
    evidence: list[ProviderEvidence] = Field(default_factory=list)
    outcome: dict[str, Any] | None = None
    failure_code: str | None = None

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        """成功需要实际回执和有类型的业务事实，不接受成功字符串。"""
        if self.status == "succeeded" and (not self.evidence or self.outcome is None):
            raise ValueError("合规成功缺少权威事实和证据")
        return self
