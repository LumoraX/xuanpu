"""供应商原始字节回调认证与独立Inbox接收，不使用员工身份。"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Request

from xuanpu_finance.dependencies import get_store
from xuanpu_finance.errors import FinanceError

router = APIRouter()


@router.post("/v1/provider-callbacks/{provider}/{configuration_version}", status_code=202)
async def provider_callback(
    request: Request, provider: str, configuration_version: int
) -> dict[str, Any]:
    """先验签，再保全回调并排原请求号查单，任何模式不直接宣称成功。"""
    registry = request.app.state.settlement_providers
    if registry is None:
        raise FinanceError("integration_unavailable", 503, "未配置供应商回调通道")
    headers = (
        "X-Finance-Timestamp",
        "X-Finance-Event-Id",
        "X-Finance-Key-Id",
        "X-Finance-Signature",
    )
    if any(len(request.headers.getlist(name)) != 1 for name in headers):
        raise FinanceError("CALLBACK_AUTH_INVALID", 401, "渠道回调认证失败")
    raw = await request.body()
    event_id, _ = registry.authenticate_callback(
        provider, configuration_version, request.headers, raw
    )
    return await get_store(request).accept_provider_event(
        provider, configuration_version, event_id, hashlib.sha256(raw).hexdigest(), raw
    )


@router.post("/v1/compliance-callbacks/{provider}/{configuration_version}", status_code=202)
async def compliance_callback(request: Request, provider: str,
                              configuration_version: int) -> dict[str, Any]:
    """独立选择合规密钥并只排原任务GET，不能从回调直接更新税务事实。"""
    registry = request.app.state.compliance_providers
    if registry is None:
        raise FinanceError("integration_unavailable", 503, "未配置合规回调通道")
    headers = ("X-Finance-Timestamp", "X-Finance-Event-Id", "X-Finance-Key-Id", "X-Finance-Signature")
    if any(len(request.headers.getlist(name)) != 1 for name in headers):
        raise FinanceError("CALLBACK_AUTH_INVALID", 401, "渠道回调认证失败")
    raw = await request.body()
    event_id, body = registry.authenticate_callback(provider, configuration_version, request.headers, raw)
    return await get_store(request).accept_compliance_event(provider, configuration_version, event_id, raw, body)
