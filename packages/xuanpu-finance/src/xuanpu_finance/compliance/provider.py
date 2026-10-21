"""复用固定HTTP与HMAC认证，合规通道不改变F2结算契约。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_finance.compliance.protocol import ComplianceResult
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.settlement.provider import (
    RESULT_SCOPE,
    ProviderRegistry,
    ProviderResponseError,
    _unique_object,
)

if TYPE_CHECKING:
    from xuanpu_finance.compliance.archive import ArchivePipeline
    from xuanpu_finance.settlement.provider_models import ProviderConfig


class ComplianceRegistry(ProviderRegistry):
    """税务沿用固定请求协议，档案通过实际文件管线执行。"""

    archive: ArchivePipeline | None = None

    def _request_config(self, request: dict[str, Any], *, submit: bool) -> ProviderConfig:
        """固定通道类型与动作匹配，禁止把税务请求送往档案渠道。"""
        config = super()._request_config(request, submit=submit)
        expected = "archive" if request["action"] in {"archive", "audit_archive"} else "tax"
        if getattr(config, "kind", None) != expected:
            raise FinanceError("PROVIDER_KIND_MISMATCH", 409, "通道类别与动作不符")
        return config

    def _validated_result(self, request: dict[str, Any], raw: bytes) -> dict[str, Any]:
        """范围、类型与权威快照均通过后才能交领域提交。"""
        try:
            parsed = ComplianceResult.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
            result = parsed.model_dump(mode="json", exclude_none=True)
            if any(result[field] != request[field] for field in RESULT_SCOPE):
                raise ValueError("合规回执范围不符")
            if parsed.status == "succeeded" and parsed.action not in {"archive", "audit_archive"}:
                from xuanpu_finance.compliance.models import TaxSnapshot

                TaxSnapshot.model_validate(parsed.outcome)
            return result
        except (ValidationError, ValueError, KeyError, RecursionError) as error:
            raise ProviderResponseError(raw) from error

    async def submit(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """归档实际发送固定ZIP，其余动作发送固定JSON。"""
        self._request_config(request, submit=True)
        if request["action"] in {"archive", "audit_archive"}:
            if self.archive is None:
                raise FinanceError("ARCHIVE_UNCONFIGURED", 503, "归档管线未配置")
            return await self.archive.submit(request)
        return await super().submit(request)

    async def query(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """已发送的所有工作只读核对，不自动重试提交。"""
        self._request_config(request, submit=False)
        if request["action"] in {"archive", "audit_archive"}:
            if self.archive is None:
                raise FinanceError("ARCHIVE_UNCONFIGURED", 503, "归档管线未配置")
            return await self.archive.query(request)
        return await super().query(request)
