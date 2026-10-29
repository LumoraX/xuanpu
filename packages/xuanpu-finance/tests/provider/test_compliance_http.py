"""税务通用通道必须保留固定scope和真实证据。"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from xuanpu_finance.compliance.protocol import ComplianceProviderConfig
from xuanpu_finance.compliance.provider import ComplianceRegistry
from xuanpu_finance.operations import request_digest
from xuanpu_finance.settlement.provider import RESULT_SCOPE, ProviderResponseError


def config(tmp_path: Any, **changes: Any) -> ComplianceProviderConfig:
    """构造明确的隔离税务通道。"""
    return ComplianceProviderConfig.model_validate(dict(
        tenant_id="t1", legal_entity_id="c1", provider="test-tax", kind="tax",
        configuration_version=1, adapter_version="v1", base_url="http://127.0.0.1:9999",
        token_file=tmp_path / "token", callback_secret_file=tmp_path / "callback",
        callback_key_id="key1", simulation=True, supports_idempotency=True,
        supports_query=True, supports_state_revision=True, **changes,
    ))


def request_body() -> dict[str, Any]:
    """查验请求使用合规目标版本并固定摘要。"""
    body = dict(protocol_version="1", tenant_id="t1", legal_entity_id="c1",
                operation_id="f3op_1", action="verify_invoice", external_request_id="ext1",
                provider="test-tax", adapter_version="v1", configuration_version=1,
                simulation=True, amount="10.00", currency="CNY",
                target=dict(id="invoice1", type="invoice", version=1), plan_digest="a" * 64,
                payload=dict(invoice_id="invoice1", content_version=1))
    body["payload_hash"] = request_digest(body)
    return body


def receipt(body: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """仅回显固定scope，不凭HTTP状态生成完成事实。"""
    return {**{key: body[key] for key in RESULT_SCOPE}, "status": "accepted", **changes}


def registry(cfg: ComplianceProviderConfig, client: httpx.AsyncClient) -> ComplianceRegistry:
    """凭据只通过内存注入。"""
    return ComplianceRegistry([cfg], client, tokens={("test-tax", 1): "token"},
                              callback_secrets={("test-tax", 1): "s" * 32})


async def test_tax_acceptance_and_readonly_query(tmp_path: Any) -> None:
    """发起一次查验，后续只能按原号GET。"""
    body, seen = request_body(), []

    def handle(req: httpx.Request) -> httpx.Response:
        """核对实际HTTP封装。"""
        seen.append((req.method, req.url.path))
        assert req.headers["Authorization"] == "Bearer token"
        if req.method == "POST":
            assert json.loads(req.content) == body
        return httpx.Response(202, json=receipt(body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        providers = registry(config(tmp_path), client)
        assert (await providers.submit(body))[0]["status"] == "accepted"
        assert (await providers.query(body))[0]["status"] == "accepted"
    assert seen == [("POST", "/v1/requests"), ("GET", "/v1/requests/ext1")]


@pytest.mark.parametrize("change", [{"tenant_id": "x"}, {"target": {"id": "x", "type": "invoice", "version": 1}}, {"simulation": False}])
async def test_tax_receipt_cannot_change_scope(tmp_path: Any, change: Any) -> None:
    """税务回执不能转给另一票或另一环境。"""
    body = request_body()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=receipt(body, **change)))) as client:
        with pytest.raises(ProviderResponseError):
            await registry(config(tmp_path), client).query(body)


async def test_tax_success_requires_actual_typed_snapshot(tmp_path: Any) -> None:
    """没有权威快照不能凭succeeded宣称查验成功。"""
    body = request_body()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=receipt(body, status="succeeded")))) as client:
        with pytest.raises(ProviderResponseError):
            await registry(config(tmp_path), client).query(body)


def test_tax_requires_revision_capability(tmp_path: Any) -> None:
    """缺失单调快照能力必须拒绝配置。"""
    values = config(tmp_path).model_dump()
    values["supports_state_revision"] = False
    with pytest.raises(ValueError):
        ComplianceProviderConfig.model_validate(values)
