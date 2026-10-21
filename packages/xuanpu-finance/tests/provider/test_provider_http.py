"""通过受限HTTP协议验证真实发送边界与供应商回执语义。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest
from xuanpu_finance.operations import request_digest
from xuanpu_finance.settlement.provider import ProviderRegistry, ProviderResponseError
from xuanpu_finance.settlement.provider_models import ProviderConfig


def configuration(tmp_path: Any, **changes: Any) -> ProviderConfig:
    """提供只指向隔离测试主机的显式通道。"""
    return ProviderConfig.model_validate(
        {
            "tenant_id": "t1",
            "legal_entity_id": "c1",
            "provider": "test-bank",
            "configuration_version": 1,
            "adapter_version": "v1",
            "base_url": "http://127.0.0.1:9999",
            "token_file": tmp_path / "token",
            "callback_secret_file": tmp_path / "callback",
            "callback_key_id": "key1",
            "simulation": True,
            "supports_idempotency": True,
            "supports_query": True,
            **changes,
        }
    )


def request_body(**changes: Any) -> dict[str, Any]:
    """构造固定请求号与精确金额的付款请求。"""
    body = {
        "protocol_version": "1",
        "tenant_id": "t1",
        "legal_entity_id": "c1",
        "operation_id": "f2op_1",
        "action": "execute_payment",
        "external_request_id": "ext1",
        "provider": "test-bank",
        "adapter_version": "v1",
        "configuration_version": 1,
        "simulation": True,
        "amount": "10.00",
        "currency": "CNY",
        "target": {"id": "claim1", "type": "expense_claim", "version": 2},
        "plan_digest": "a" * 64,
        "payee_ref": {"kind": "employee_account", "id": "account1", "version": 1},
        "provider_account_id": "bank-account1",
        **changes,
    }
    body["payload_hash"] = request_digest(body)
    return body


def response_body(
    request: dict[str, Any], status: str = "accepted", **changes: Any
) -> dict[str, Any]:
    """回显冻结范围，业务回执字段由各案例添加。"""
    keys = (
        "protocol_version",
        "tenant_id",
        "legal_entity_id",
        "operation_id",
        "action",
        "external_request_id",
        "provider",
        "adapter_version",
        "configuration_version",
        "payload_hash",
        "simulation",
        "amount",
        "currency",
        "target",
        "plan_digest",
    )
    return {**{key: request[key] for key in keys}, "status": status, **changes}


def registry(config: ProviderConfig, client: httpx.AsyncClient) -> ProviderRegistry:
    """使用注入的客户端和内存凭据，不读取宿主配置。"""
    return ProviderRegistry(
        [config],
        client,
        tokens={("test-bank", 1): "token"},
        callback_secrets={("test-bank", 1): "secret"},
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://u:p@example.com",
        "https://example.com/path",
        "https://example.com/?q=x",
        "https://example.com/#x",
    ],
)
def test_provider_origin_rejects_untrusted_urls(tmp_path: Any, url: str) -> None:
    """通道不能带凭据、任意路径或不受控HTTP目标。"""
    with pytest.raises(ValueError):
        configuration(tmp_path, base_url=url)


async def test_accepted_is_not_payment_success(tmp_path: Any) -> None:
    """HTTP202仅表达受理，且实际HTTP固定幂等请求号。"""
    body = request_body()

    def handle(request: httpx.Request) -> httpx.Response:
        """核对真实客户端构造的请求内容。"""
        assert request.method == "POST" and request.url.path == "/v1/requests"
        assert request.headers["Idempotency-Key"] == "ext1"
        assert request.headers["Authorization"] == "Bearer token"
        assert json.loads(request.content) == body
        return httpx.Response(202, json=response_body(body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result, raw = await registry(configuration(tmp_path), client).submit(body)
    assert result["status"] == "accepted" and json.loads(raw)["status"] == "accepted"


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "other"},
        {"amount": "9.00"},
        {"currency": "USD"},
        {"simulation": False},
        {"configuration_version": 2},
        {"payload_hash": "b" * 64},
    ],
)
async def test_response_must_match_frozen_request(tmp_path: Any, changes: dict[str, Any]) -> None:
    """任何归属、金额或环境差异都拒绝作为实际结果。"""
    body = request_body()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_body(body, **changes))
        )
    ) as client:
        with pytest.raises(ProviderResponseError) as error:
            await registry(configuration(tmp_path), client).query(body)
    assert error.value.raw


async def test_success_needs_real_transaction_and_evidence(tmp_path: Any) -> None:
    """成功字符串没有交易与回单不能改变付款事实。"""
    body = request_body()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_body(body, "succeeded"))
        )
    ) as client:
        with pytest.raises(ProviderResponseError):
            await registry(configuration(tmp_path), client).query(body)


async def test_query_not_found_preserves_raw_status(tmp_path: Any) -> None:
    """查无订单仍交由持久队列保持未知，适配器不重发。"""
    body = request_body()

    def handle(request: httpx.Request) -> httpx.Response:
        """只能对相同外部请求号执行GET。"""
        assert request.method == "GET" and request.url.path == "/v1/requests/ext1"
        return httpx.Response(404, json=response_body(body, "not_found"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result, _ = await registry(configuration(tmp_path), client).query(body)
    assert result["status"] == "not_found"


def test_callback_authenticates_raw_bytes_timestamp_and_event(tmp_path: Any) -> None:
    """签名覆盖原字节而非重新序列化JSON。"""
    body = request_body()
    raw = json.dumps(response_body(body)).encode()
    timestamp, event = str(int(time.time())), "event1"
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + event.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Finance-Timestamp": timestamp,
        "X-Finance-Event-Id": event,
        "X-Finance-Key-Id": "key1",
        "X-Finance-Signature": "sha256=" + signature,
    }
    client = httpx.AsyncClient()
    providers = registry(configuration(tmp_path), client)
    assert providers.authenticate_callback("test-bank", 1, headers, raw)[0] == event
    with pytest.raises(Exception):
        providers.authenticate_callback("test-bank", 1, headers, raw + b" ")


def evidence() -> list[dict[str, str]]:
    """明确标记模拟渠道回单证据。"""
    return [
        {
            "id": "receipt1",
            "sha256": "c" * 64,
            "media_type": "application/pdf",
            "object_ref": "simulation/receipt1",
        }
    ]


async def test_successful_payment_has_transaction_and_preserves_evidence(tmp_path: Any) -> None:
    """订单与资金交易分别保留，成功必须来自完整渠道回执。"""
    body = request_body()
    result_body = response_body(
        body, "succeeded", external_order_id="order1", transaction_id="tx1", evidence=evidence()
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result_body))
    ) as client:
        result, raw = await registry(configuration(tmp_path), client).query(body)
    assert result["transaction_id"] == "tx1" and result["evidence"] == evidence()
    assert json.loads(raw) == result_body


@pytest.mark.parametrize("field", ["ledger_id", "accounting_period"])
async def test_posting_success_matches_configured_ledger_and_period(
    tmp_path: Any, field: str
) -> None:
    """凭证必须属于计划账套和期间，不能临时换账。"""
    body = request_body(action="submit_posting", ledger_id="ledger1", accounting_period="2026-10")
    result_body = response_body(
        body,
        "succeeded",
        ledger_id="ledger1",
        accounting_period="2026-10",
        voucher_id="voucher1",
        evidence=evidence(),
    )
    result_body[field] = "wrong"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result_body))
    ) as client:
        with pytest.raises(ProviderResponseError):
            await registry(configuration(tmp_path), client).query(body)


async def test_disabled_provider_still_queries_but_cannot_submit(tmp_path: Any) -> None:
    """停用配置继续核对在途结果，同时拒绝新写。"""
    body = request_body()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_body(body)))
    ) as client:
        providers = registry(configuration(tmp_path, enabled=False), client)
        assert (await providers.query(body))[0]["status"] == "accepted"
        with pytest.raises(Exception):
            await providers.submit(body)


async def test_real_loopback_http_request_and_query(tmp_path: Any) -> None:
    """实际TCP服务验证提交和同号GET，不依赖MockTransport。"""
    import asyncio

    body, seen = request_body(), []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """测试服务读取HTTP原文并返回模拟受理回执。"""
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        seen.append(lines[0])
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        size = int(headers.get("Content-Length", "0"))
        if size:
            assert json.loads(await reader.readexactly(size)) == body
        raw = json.dumps(response_body(body)).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
            + str(len(raw)).encode()
            + b"\r\n\r\n"
            + raw
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            providers = registry(
                configuration(tmp_path, base_url=f"http://127.0.0.1:{port}"), client
            )
            assert (await providers.submit(body))[0]["status"] == "accepted"
            assert (await providers.query(body))[0]["status"] == "accepted"
    finally:
        server.close()
        await server.wait_closed()
    assert seen == ["POST /v1/requests HTTP/1.1", "GET /v1/requests/ext1 HTTP/1.1"]


@pytest.mark.parametrize(
    "offset,event,key",
    [
        (-301, "event1", "key1"),
        (301, "event1", "key1"),
        (0, "event.with.dot", "key1"),
        (0, "event1", "wrong"),
    ],
)
async def test_callback_rejects_stale_future_or_wrong_auth_metadata(
    tmp_path: Any, offset: int, event: str, key: str
) -> None:
    """签名正确也不能绕过时间窗或配置密钥标识。"""
    raw = json.dumps(response_body(request_body())).encode()
    timestamp = str(int(time.time()) + offset)
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + event.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Finance-Timestamp": timestamp,
        "X-Finance-Event-Id": event,
        "X-Finance-Key-Id": key,
        "X-Finance-Signature": "sha256=" + signature,
    }
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception):
            registry(configuration(tmp_path), client).authenticate_callback(
                "test-bank", 1, headers, raw
            )


async def test_duplicate_response_fields_are_rejected_with_raw_evidence(tmp_path: Any) -> None:
    """不能让重复JSON身份键依赖不同解析器的先后覆盖。"""
    body = request_body()
    raw = (
        json.dumps(response_body(body))
        .replace('"tenant_id": "t1"', '"tenant_id": "other", "tenant_id": "t1"')
        .encode()
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
    ) as client:
        with pytest.raises(ProviderResponseError) as error:
            await registry(configuration(tmp_path), client).query(body)
    assert error.value.raw == raw


async def test_signed_callback_rejects_boolean_configuration_version(tmp_path: Any) -> None:
    """JSON布尔值不能因为Python相等规则冒充配置版本。"""
    raw = json.dumps(response_body(request_body(), configuration_version=True)).encode()
    timestamp, event = str(int(time.time())), "event1"
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + event.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Finance-Timestamp": timestamp,
        "X-Finance-Event-Id": event,
        "X-Finance-Key-Id": "key1",
        "X-Finance-Signature": "sha256=" + signature,
    }
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception):
            registry(configuration(tmp_path), client).authenticate_callback(
                "test-bank", 1, headers, raw
            )


@pytest.mark.parametrize("error_type", [httpx.ReadError, httpx.RemoteProtocolError])
async def test_partial_response_survives_interrupted_stream(
    tmp_path: Any, error_type: type[httpx.TransportError]
) -> None:
    """收到回执前缀后断流也保全原字节，结果只能未知。"""
    partial = b'{"status":"succeeded","transaction_id":'

    class InterruptedStream(httpx.AsyncByteStream):
        """先返回真实收到的字节，再模拟TCP或HTTP协议中断。"""

        async def __aiter__(self):
            """分块读取失败不能抹去已接收的回执证据。"""
            yield partial
            raise error_type("模拟回执流中断")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=InterruptedStream())
        )
    ) as client:
        with pytest.raises(ProviderResponseError) as caught:
            await registry(configuration(tmp_path), client).query(request_body())
    assert caught.value.raw == partial
    assert caught.value.code == "PROVIDER_NETWORK_UNKNOWN"


async def test_deeply_nested_response_keeps_raw_and_becomes_invalid(tmp_path: Any) -> None:
    """字节有界但嵌套超深也应保全回执，不能击停整个查单worker。"""
    raw = b"[" * 10000 + b"0" + b"]" * 10000
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
    ) as client:
        with pytest.raises(ProviderResponseError) as error:
            await registry(configuration(tmp_path), client).query(request_body())
    assert error.value.raw == raw


async def test_deeply_nested_signed_callback_is_controlled_error(tmp_path: Any) -> None:
    """合法签名不能使过深JSON逃逸为未处理异常。"""
    from xuanpu_finance.errors import FinanceError

    raw = b"[" * 10000 + b"0" + b"]" * 10000
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"secret", timestamp.encode() + b".deep." + raw, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Finance-Timestamp": timestamp,
        "X-Finance-Event-Id": "deep",
        "X-Finance-Key-Id": "key1",
        "X-Finance-Signature": "sha256=" + signature,
    }
    async with httpx.AsyncClient() as client:
        with pytest.raises(FinanceError):
            registry(configuration(tmp_path), client).authenticate_callback(
                "test-bank", 1, headers, raw
            )
