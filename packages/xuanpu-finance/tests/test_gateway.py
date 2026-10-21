"""平台身份和文件桥的实际 HTTP 协议与异常边界。"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.gateway import PlatformGateway
from xuanpu_finance.models import FileRef, FinancePrincipal


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant_id": "other", "user_id": "alice", "active": True},
        {"tenant_id": "acme", "user_id": "boss", "active": True},
        {"tenant_id": "acme", "user_id": "alice", "active": "true"},
    ],
)
async def test_online_identity_must_match_subject(payload: dict[str, Any]) -> None:
    """成功HTTP不能掩盖目录回复主体不符。"""
    async with httpx.AsyncClient(
        base_url="https://platform.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as client:
        gateway = PlatformGateway(client, tenant_tokens={"acme": "test-api-key"})
        with pytest.raises(FinanceError):
            await gateway.active(FinancePrincipal("acme", "alice"))


async def test_file_bridge_pins_version_and_checksum() -> None:
    """实际请求带版本和摘要，返回原始字节而不是URL。"""
    data = b"finance original"
    digest = hashlib.sha256(data).hexdigest()

    def transport(request: httpx.Request) -> httpx.Response:
        """核对可信代理身份和固定文件参数。"""
        assert request.headers["X-Xuanpu-Act-As"] == "alice"
        assert request.url.params["revision"] == "2" and request.url.params["sha256"] == digest
        return httpx.Response(200, content=data, headers={"Content-Type": "application/xml"})

    async with httpx.AsyncClient(
        base_url="https://platform.example", transport=httpx.MockTransport(transport)
    ) as client:
        gateway = PlatformGateway(client, tenant_tokens={"acme": "test-api-key"})
        result, mime, name = await gateway.download(
            FinancePrincipal("acme", "alice"), FileRef(file_id="f1", revision=2, sha256=digest)
        )
    assert result == data and mime == "application/xml" and name == "f1"


@pytest.mark.parametrize(
    "limit,digest", [(1, hashlib.sha256(b"original").hexdigest()), (100, "a" * 64)]
)
async def test_file_bridge_rejects_oversize_and_changed_bytes(limit: int, digest: str) -> None:
    """超限或字节变化不交给解析器。"""
    async with httpx.AsyncClient(
        base_url="https://platform.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"original")),
    ) as client:
        gateway = PlatformGateway(
            client, tenant_tokens={"acme": "test-api-key"}, max_file_bytes=limit
        )
        with pytest.raises(FinanceError):
            await gateway.download(
                FinancePrincipal("acme", "alice"), FileRef(file_id="f1", revision=1, sha256=digest)
            )
