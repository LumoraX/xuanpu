"""远端 MCP 连接器的部件单测：凭据传输 ``VaultAuthTransport`` 与运行实例 ``RemoteRun``。

不起真的服务：内层传输用 ``httpx.MockTransport``，保管库用记下每次取值的假实现。经平台
走一遍的用例在 ``test_remote_mcp.py``。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from xuanpu_server.errors import InvalidRequestError, PackageInvalidError, SecretMissingError
from xuanpu_server.runtime.remote_mcp import UNREDACTABLE, RemoteRun, VaultAuthTransport
from xuanpu_spec.manifest import HttpAuth

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

URL = "https://mcp.example.com/mcp"
SECRET = "secret-value-0123"


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


@pytest.mark.parametrize("value", ["abc\r\nX-Evil: 1", "abc\ndef", "abc\x00def"])
async def test_vault_transport_refuses_values_that_break_the_header(value: str) -> None:
    """密钥取值里有 CR、LF 或空字符：请求不发出，报 invalid_request，报错里没有取值。"""
    inner, received = recording_inner()
    vault = FakeVault({"docs-token": value})
    transport = vault_transport(inner, vault, HttpAuth(type="bearer", secret="docs-token"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(InvalidRequestError, match="docs-token") as caught:
            await client.get("https://mcp.example.com/mcp")
    assert received == []
    assert "abc" not in str(caught.value)
    assert caught.value.code == "invalid_request"


# ---------------------------------------------------------------------------
# RemoteRun：脱敏、截断与收尾
# ---------------------------------------------------------------------------


def mcp_handler(
    call: Callable[[httpx.Request, dict[str, Any]], dict[str, Any]], seen: list[str]
) -> Callable[[httpx.Request], httpx.Response]:
    """一个最小的 MCP 端点（给 MockTransport 用）：握手与列工具照常，``tools/call`` 交给 ``call``。

    ``seen`` 记下每次请求的 HTTP 方法与 JSON-RPC 方法。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            seen.append("DELETE")
            return httpx.Response(204)
        message = json.loads(request.content)
        seen.append(message["method"])
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "unit"},
            }
            headers = {"Mcp-Session-Id": "s1"}
        elif message["method"] == "tools/list":
            result, headers = {"tools": [{"name": "t"}]}, {}
        else:
            result, headers = call(request, message["params"]), {}
        payload = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        return httpx.Response(200, json=payload, headers=headers)

    return handler


class ClosingInner(httpx.AsyncBaseTransport):
    """记下被关闭了几次的内层传输，请求交给 MockTransport。"""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._mock = httpx.MockTransport(handler)
        self.closed = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """交给 MockTransport。"""
        return await self._mock.handle_async_request(request)

    async def aclose(self) -> None:
        """记下关闭。"""
        self.closed += 1


def text_result(text: str) -> dict[str, Any]:
    """只有一段文本的工具结果。"""
    return {"content": [{"type": "text", "text": text}]}


def remote_run(vault: FakeVault, inner: httpx.AsyncBaseTransport | None = None) -> RemoteRun:
    """指向 ``URL``、bearer 凭据取 ``docs-token`` 的运行实例。"""
    return RemoteRun(
        name="docs",
        url=URL,
        auth=HttpAuth(type="bearer", secret="docs-token"),
        tenant_id="acme",
        vault=vault,  # type: ignore[arg-type]
        inner_transport=inner,
    )


async def test_result_is_redacted_before_it_is_truncated() -> None:
    """结果超长、密钥正好跨在 64 KiB 的截断处：先脱敏再截，截断处不留半截密钥。"""
    seen: list[str] = []
    text = "a" * (64 * 1024 - 5) + SECRET + "b" * 100
    inner = ClosingInner(mcp_handler(lambda request, params: text_result(text), seen))
    run = remote_run(FakeVault({"docs-token": SECRET}), inner)
    await run.connect(request_timeout=5)
    try:
        output, is_error = await run.invoker("t")({})
    finally:
        await run.close()
    assert is_error is False
    assert SECRET[:5] not in output
    assert output[64 * 1024 - 5 : 64 * 1024] == "***bb"
    assert "已截断" in output


async def test_unredactable_text_is_withheld_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """要脱敏时密钥已取不到（调用途中被删）：整段换成 UNREDACTABLE，并记一条告警。"""
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    vault = FakeVault({"docs-token": SECRET})

    def call(request: httpx.Request, params: dict[str, Any]) -> dict[str, Any]:
        """回显凭据，同时把密钥删掉。"""
        vault.values.clear()
        return text_result(f"收到 {request.headers['authorization']}")

    run = remote_run(vault, ClosingInner(mcp_handler(call, seen)))
    await run.connect(request_timeout=5)
    try:
        assert await run.invoker("t")({}) == (UNREDACTABLE, False)
    finally:
        await run.close()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("密钥已取不到" in message for message in warnings)
    assert SECRET not in caplog.text


async def test_default_transport_ignores_environment_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认的内层传输不读环境里的证书与代理配置：环境里指了一个不存在的证书文件也不受影响。"""
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    run = remote_run(FakeVault({}))
    await run.close()


async def test_close_closes_the_transport_even_if_never_connected() -> None:
    """没连上（或根本没开始连）就关闭：传输照样关掉，连接池不泄漏。"""
    seen: list[str] = []
    inner = ClosingInner(mcp_handler(lambda request, params: text_result(""), seen))
    run = remote_run(FakeVault({"docs-token": SECRET}), inner)
    await run.close()
    assert (inner.closed, seen) == (1, [])


async def test_close_after_the_secret_was_deleted(caplog: pytest.LogCaptureFixture) -> None:
    """关闭前密钥被删：结束会话的 DELETE 发不出去，记一条告警，传输照样关掉。"""
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    vault = FakeVault({"docs-token": SECRET})
    inner = ClosingInner(mcp_handler(lambda request, params: text_result(""), seen))
    run = remote_run(vault, inner)
    await run.connect(request_timeout=5)
    vault.values.clear()
    await run.close()
    assert inner.closed == 1
    assert "DELETE" not in seen
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("没能结束远端会话（secret_missing）" in message for message in warnings)


def test_url_that_httpx_cannot_parse_is_a_package_error() -> None:
    """地址通过了规范校验、httpx 却解析不了（这里是不合法的国际化域名）：报业务包不合法。"""
    with pytest.raises(PackageInvalidError, match="'docs' 的 url 不是合法的地址"):
        RemoteRun(
            name="docs",
            url="https://例-.com/mcp",
            auth=None,
            tenant_id="acme",
            vault=FakeVault({}),  # type: ignore[arg-type]
        )
