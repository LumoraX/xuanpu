"""远端 MCP 连接器（``transport: remote``）：经 MCP Streamable HTTP 访问部署在别处的 server。

客户端用内核稳定层的 ``McpHttpClient``。凭据按规则 2 处理——清单里只有密钥名，明文只在
出口那一刻取出：

- 不经客户端的 ``headers`` 注入（那会让明文在客户端对象里一直待到连接器关闭）；
- 而是给客户端一个自定义的 httpx 传输 ``VaultAuthTransport``，每个请求发出之前从保管库现取
  凭据加到请求头上。密钥轮换即时生效，客户端与传输对象里都不留明文；
- 只给发往清单 ``url`` 同源（协议、主机、端口）的请求加凭据，别的请求一律拒绝发出。

远端每次请求都拿到凭据，它回的任何文本都可能把凭据带回来：交给模型与审计的文本、日志里的
排障线索都按值脱敏。脱敏用的明文同样是现取现用。远端 MCP 不进沙盒：它本来就是一次网络调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import taifeng

from xuanpu_server.errors import PackageInvalidError, PlatformError
from xuanpu_server.redaction import head, redact
from xuanpu_server.runtime.http_api import ConnectorCallError, auth_header
from xuanpu_server.runtime.mcp_connectors import (
    MAX_ERROR_CHARS,
    described,
    render_text,
    truncate,
    unexpected_failure,
)

if TYPE_CHECKING:
    from xuanpu_spec.manifest import HttpAuth

    from xuanpu_server.runtime.mcp_connectors import Invoke
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)

# 预料之中的调用失败：文本有用，脱敏后交给模型与审计
_EXPECTED_FAILURES = (taifeng.McpToolError, httpx.HTTPError, RuntimeError, OSError)
UNREDACTABLE = "[已隐去：密钥这时已取不到，无法按值脱敏]"
"""要脱敏时密钥已经取不到（例如刚被删掉）：无从按值替换，整段换成这句，不冒险原样写出。"""


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """URL 的源：协议、主机、端口（默认端口记为 None）。"""
    return url.scheme, url.host, url.port


class CredentialScopeError(httpx.TransportError):
    """请求不是发往清单声明的连接器地址：凭据传输拒绝发出。"""


class VaultAuthTransport(httpx.AsyncBaseTransport):
    """每次请求发出时从保管库取凭据加到请求头；客户端对象里不长期持有明文（规则 2）。

    只放行发往 ``url`` 同源的请求：凭据只会发给清单声明的那台主机。内核客户端只向这一个
    端点发请求，也不跟随重定向；这道检查挡住以后的改动意外把带凭据的请求引到别处。
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        url: str,
        auth: HttpAuth | None,
        tenant_id: str,
        vault: SecretVault,
    ) -> None:
        """
        Args:
            inner: 真正发请求的传输。
            url: 清单里的连接器地址，只有发往它同源的请求才放行。
            auth: 凭据注入方式；None 表示不加凭据。
            tenant_id: 所属租户，取密钥时用。
            vault: 密钥保管库。
        """
        self._inner = inner
        self._origin = _origin(httpx.URL(url))
        self._auth = auth
        self._tenant_id = tenant_id
        self._vault = vault
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """加上凭据头后交给内层传输。

        Raises:
            CredentialScopeError: 请求不是发往清单声明的地址。
            SecretMissingError: 密钥这时已取不到；请求不发出。
        """
        if _origin(request.url) != self._origin:
            raise CredentialScopeError(
                "请求的地址与清单声明的连接器地址不同源，拒绝发出", request=request
            )
        if self._auth is not None:
            name, value = await auth_header(
                self._auth, tenant_id=self._tenant_id, vault=self._vault
            )
            request.headers[name] = value
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        """关闭内层传输。重复调用无害。"""
        async with self._close_lock:
            if self._closed:
                return
            await self._inner.aclose()
            self._closed = True


@dataclass
class RemoteRun:
    """一个 remote 连接器的客户端与排障线索。

    Attributes:
        name: 连接器名。
        url: MCP 端点地址（规范已校验：带主机名的 http(s) 地址，不带用户名与口令）。
        auth: 凭据注入方式；None 表示不需要凭据。
        tenant_id: 所属租户。
        vault: 密钥保管库。
        client: 握手成功后的客户端；没连上时为 None。
        inner_transport: 真正发请求的传输，测试时注入；None 时用不读环境配置的
            ``httpx.AsyncHTTPTransport``。
        transport: 给客户端用的凭据传输（包住 ``inner_transport``），由 ``close`` 关掉。
    """

    name: str
    url: str
    auth: HttpAuth | None
    tenant_id: str
    vault: SecretVault
    client: taifeng.McpHttpClient | None = None
    inner_transport: httpx.AsyncBaseTransport | None = None
    transport: VaultAuthTransport = field(init=False)

    def __post_init__(self) -> None:
        """备好凭据传输。

        Raises:
            PackageInvalidError: 地址通过了规范校验、httpx 却解析不了。先解析再建传输，失败时
                不会留下建了一半的传输。
        """
        try:
            httpx.URL(self.url)
        except httpx.InvalidURL:
            raise PackageInvalidError(f"连接器 {self.name!r} 的 url 不是合法的地址") from None
        # 不读环境里的代理与证书配置（与 HTTP 连接器一致）：出口走哪里由部署决定
        inner = self.inner_transport
        if inner is None:
            inner = httpx.AsyncHTTPTransport(trust_env=False)
        self.transport = VaultAuthTransport(
            inner,
            url=self.url,
            auth=self.auth,
            tenant_id=self.tenant_id,
            vault=self.vault,
        )

    async def connect(self, *, request_timeout: float) -> taifeng.McpHttpClient:
        """握手。握手一成功就把客户端记下来，之后的失败由 ``close()`` 关掉它。

        不开 GET 推送流（``listen_notifications=False``）：运行时的工具在组装时定下，之后不随
        ``tools/list_changed`` 增减（闸门与权限策略是按那时的工具生成的），推送流收到的通知
        用不上；不开就省掉每个运行时一条常驻连接与断线重连。server 发起的请求仍可夹在各次
        POST 的响应流里送达。
        """
        self.client = await taifeng.McpHttpClient.connect(
            self.url,
            transport=self.transport,
            request_timeout_seconds=request_timeout,
            listen_notifications=False,
        )
        return self.client

    async def redact(self, text: str) -> str:
        """按当前的密钥明文脱敏：现取现用、用完即弃，不在对象里留存。

        密钥这时已取不到就无从按值替换，整段换成 ``UNREDACTABLE``。轮换恰好发生在请求与
        脱敏之间时，旧值认不出来——这个窗口只有一次请求那么长。
        """
        if self.auth is None:
            return text
        try:
            secret = await self.vault.get(self.tenant_id, self.auth.secret)
        except PlatformError as exc:
            logger.warning(
                "连接器 %s 要按值脱敏时密钥已取不到（%s）：这段文本整段不记", self.name, exc.code
            )
            return UNREDACTABLE
        return redact(text, (secret,))

    async def diagnostics(self, exc: BaseException | None, *, settle: bool = False) -> str:
        """脱敏后的排障线索：异常类型与文本，连上了的话再加 server 自报的身份。

        各段都用 repr 写出，远端回的换行不会在日志里伪造出新的一行。没有进程要等，
        ``settle`` 不起作用。
        """
        parts: list[str] = []
        if exc is not None:
            parts.append(described(exc, await self.redact(str(exc))))
        if self.client is not None:
            info = await self.redact(json.dumps(self.client.server_info, ensure_ascii=False))
            parts.append(f"serverInfo {info!r}")
        return "；".join(parts) or "没有更多线索"

    def invoker(self, tool: str) -> Invoke:
        """调用这个连接器的某个工具。须在握手成功之后调用。

        结果与错误文本都按值脱敏后才交给模型与审计。平台侧的失败（例如密钥刚被删掉，请求
        没有发出）原样上抛，由闸门按错误码记下；其余失败一律在这里接住（见 ``_call_failed``）。
        """
        client = self.client
        assert client is not None

        async def invoke(args: dict[str, Any]) -> tuple[str, bool]:
            """调一次工具，返回脱敏、截断后的文本与是否出错。

            Raises:
                PlatformError: 平台侧的失败（如密钥刚被删掉），原样上抛。
                ConnectorCallError: 其余调用失败（文本已脱敏，没预料到的只有异常类型）。
            """
            try:
                result = await client.call_tool(tool, args)
            except PlatformError:
                raise
            except Exception as exc:
                # 内核客户端的失败不止 McpToolError：远端回的响应形状不对时，解析途中也会冒出
                # ValueError、TypeError、UnicodeDecodeError 之类，文本里可能就是远端回显的凭据
                raise await self._call_failed(tool, exc) from None
            # 先拿全文脱敏再截断：先截的话，截断处正好切开密钥时前半截就认不出来了
            rendered, is_error = render_text(result)
            return truncate(await self.redact(rendered)), is_error

        return invoke

    async def _call_failed(self, tool: str, exc: Exception) -> ConnectorCallError:
        """记下一次调用失败（日志按值脱敏），返回交给闸门的错误。

        预料之中的失败（内核的 ``McpToolError``、传输与连接错误）交出脱敏后的文本，对模型
        有用；没预料到的异常只交出异常类型——它们的文本里是什么说不准。
        """
        text = await self.redact(str(exc))
        logger.warning("连接器 %s 的工具 %s 调用失败：%s", self.name, tool, described(exc, text))
        if isinstance(exc, _EXPECTED_FAILURES):
            return ConnectorCallError(head(text, MAX_ERROR_CHARS))
        return ConnectorCallError(unexpected_failure(exc))

    async def close(self) -> None:
        """关闭客户端（它会 DELETE 结束会话），再关掉凭据传输与它的连接池。

        关闭前密钥被删掉时，DELETE 经凭据传输取不到凭据、抛 ``SecretMissingError``。内核
        ``McpHttpClient.close()`` 只接 ``httpx.HTTPError``，遇到它会跳过关闭自己的 httpx 客户端
        （内核侧的问题，由缺口清单跟进）。所以这里接住平台侧的失败记一条告警，并且无论如何
        都关掉我们自己建的传输——连接池在传输里，随之关闭。
        """
        try:
            if self.client is not None:
                await self.client.close()
        except PlatformError as exc:
            logger.warning("连接器 %s 关闭时没能结束远端会话（%s）", self.name, exc.code)
        finally:
            await self.transport.aclose()


__all__ = ["UNREDACTABLE", "CredentialScopeError", "RemoteRun", "VaultAuthTransport"]
