"""可替换HTTP付款与记账通道的严格配置和回执契约。"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — Pydantic运行时解析
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderConfig(BaseModel):
    """显式固定公司、版本与隔离环境，不从宿主发现凭据。"""

    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=128)
    legal_entity_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    configuration_version: int = Field(ge=1, strict=True)
    adapter_version: str = Field(min_length=1, max_length=128)
    base_url: str
    token_file: Path
    callback_secret_file: Path
    callback_key_id: str = Field(min_length=1, max_length=128)
    simulation: bool = Field(default=False, strict=True)
    enabled: bool = Field(default=True, strict=True)
    supports_idempotency: bool = Field(default=False, strict=True)
    supports_query: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def origin_only(self) -> Self:
        """正式端点仅HTTPS，模拟HTTP只准明确测试主机。"""
        parts = urlsplit(self.base_url)
        test_hosts = {
            "localhost",
            "127.0.0.1",
            "::1",
            "host.docker.internal",
            "finance-test-provider",
        }
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or (
                parts.scheme == "http" and (not self.simulation or parts.hostname not in test_hosts)
            )
        ):
            raise ValueError("通道必须是固定HTTPS origin或隔离测试HTTP主机")
        self.base_url = self.base_url.rstrip("/")
        return self

    def public_descriptor(self) -> dict[str, Any]:
        """返回可持久校验的版本摘要，凭据及路径不进入业务存储。"""
        return self.model_dump(exclude={"token_file", "callback_secret_file", "callback_key_id"})


class ProviderTarget(BaseModel):
    """冻结业务目标和版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    type: Literal["expense_claim", "payable", "posting"]
    version: int = Field(ge=1)


class ProviderEvidence(BaseModel):
    """保存渠道声明的证据引用，不自动访问外部URL。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=128)
    object_ref: str = Field(min_length=1, max_length=2048)


class ProviderResult(BaseModel):
    """渠道状态与HTTP状态分离，终态成功须具备真实业务标识。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    protocol_version: Literal["1"]
    tenant_id: str
    legal_entity_id: str
    operation_id: str
    action: Literal["execute_payment", "submit_posting", "reverse_posting"]
    external_request_id: str
    provider: str
    adapter_version: str
    configuration_version: int = Field(ge=1)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    simulation: bool
    amount: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,15})\.[0-9]{2}$")
    currency: Literal["CNY"]
    target: ProviderTarget
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["accepted", "pending", "succeeded", "failed", "unknown", "not_found"]
    external_order_id: str | None = None
    transaction_id: str | None = None
    ledger_id: str | None = None
    accounting_period: str | None = None
    voucher_id: str | None = None
    evidence: list[ProviderEvidence] = Field(default_factory=list)
    failure_code: str | None = None

    @model_validator(mode="after")
    def successful_evidence(self) -> Self:
        """没有交易或凭证、没有证据的成功回执不具备完成语义。"""
        if self.amount == "0.00":
            raise ValueError("外部动作金额必须为正")
        if self.status == "succeeded":
            if not self.evidence:
                raise ValueError("成功回执缺少证据")
            if self.action == "execute_payment":
                if not self.external_order_id or not self.transaction_id:
                    raise ValueError("付款成功缺少订单或交易ID")
            elif not self.ledger_id or not self.accounting_period or not self.voucher_id:
                raise ValueError("记账成功缺少账套、期间或凭证ID")
        return self
