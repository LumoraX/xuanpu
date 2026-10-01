"""远端 MCP 连接器的部件单测：凭据传输 ``VaultAuthTransport`` 与运行实例 ``RemoteRun``。

不起真的服务：内层传输用 ``httpx.MockTransport``，保管库用记下每次取值的假实现。经平台
走一遍的用例在 ``test_remote_mcp.py``。
"""

from __future__ import annotations

import httpx
import pytest
from xuanpu_server.errors import SecretMissingError
from xuanpu_server.runtime.remote_mcp import VaultAuthTransport
from xuanpu_spec.manifest import HttpAuth


class FakeVault:
    """记下每次取值的保管库。"""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.reads: list[tuple[str, str]] = []

    async def get(self, tenant_id: str, name: str) -> str:
        """取明文；没有就报缺密钥。"""
        self.reads.append((tenant_id, name))
        if name not in self.values:
            raise SecretMissingError(f"密钥 {name!r} 尚未配置")
        return self.values[name]


def recording_inner() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """一个记下收到的请求、一律回 200 的内层传输。"""
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler), received


def vault_transport(
    inner: httpx.AsyncBaseTransport, vault: FakeVault, auth: HttpAuth | None
) -> VaultAuthTransport:
    """指向 ``https://mcp.example.com/mcp`` 的凭据传输。"""
    return VaultAuthTransport(
        inner, url="https://mcp.example.com/mcp", auth=auth, tenant_id="acme", vault=vault
    )


async def test_vault_transport_fetches_the_secret_for_every_request() -> None:
    """每次请求都从保管库现取：轮换后下一次请求就是新值；传输对象里没有明文。"""
    inner, received = recording_inner()
    vault = FakeVault({"docs-token": "first-value"})
    transport = vault_transport(inner, vault, HttpAuth(type="bearer", secret="docs-token"))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://mcp.example.com/mcp", json={})
        assert "first-value" not in repr(vars(transport))
        vault.values["docs-token"] = "second-value"
        await client.post("https://mcp.example.com/mcp", json={})
    assert [request.headers["authorization"] for request in received] == [
        "Bearer first-value",
        "Bearer second-value",
    ]
    assert vault.reads == [("acme", "docs-token"), ("acme", "docs-token")]


async def test_vault_transport_header_style_and_no_auth() -> None:
    """header 方式放到指定的头；不声明凭据时什么也不加，也不读保管库。"""
    inner, received = recording_inner()
    vault = FakeVault({"docs-key": "k-1"})
    header = HttpAuth(type="header", secret="docs-key", header="X-Api-Key")
    async with httpx.AsyncClient(transport=vault_transport(inner, vault, header)) as client:
        await client.get("https://mcp.example.com/mcp")
    async with httpx.AsyncClient(transport=vault_transport(inner, vault, None)) as client:
        await client.get("https://mcp.example.com/mcp")
    assert received[0].headers["x-api-key"] == "k-1"
    assert "authorization" not in received[0].headers
    assert ("x-api-key" in received[1].headers, "authorization" in received[1].headers) == (
        False,
        False,
    )
    assert vault.reads == [("acme", "docs-key")]


async def test_vault_transport_refuses_other_origins() -> None:
    """发往清单地址之外的请求一律拒绝：凭据只会发给声明的那台主机。"""
    inner, received = recording_inner()
    vault = FakeVault({"docs-token": "secret-value"})
    transport = vault_transport(inner, vault, HttpAuth(type="bearer", secret="docs-token"))
    async with httpx.AsyncClient(transport=transport) as client:
        for url in ("https://evil.example.com/mcp", "http://mcp.example.com/mcp"):
            with pytest.raises(httpx.TransportError, match="清单"):
                await client.get(url)
    assert received == []
    assert vault.reads == []


async def test_vault_transport_missing_secret_and_close() -> None:
    """密钥取不到：请求不发出，缺密钥的错误照常上抛；关闭会关掉内层传输，重复关闭无害。"""
    closed: list[str] = []

    class Inner(httpx.AsyncBaseTransport):
        """记下关闭的内层传输。"""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("不该发出请求")

        async def aclose(self) -> None:
            closed.append("inner")

    transport = vault_transport(Inner(), FakeVault({}), HttpAuth(type="bearer", secret="gone"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SecretMissingError, match="gone"):
            await client.get("https://mcp.example.com/mcp")
    await transport.aclose()
    assert closed == ["inner"]
