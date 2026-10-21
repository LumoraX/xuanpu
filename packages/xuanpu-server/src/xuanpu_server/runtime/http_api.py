"""HTTP 连接器：按 OpenAPI 的 operation 发请求。

这是平台到外部系统的一个出口，几条约束都是为了让它不可被模型或被调用方牵着走：

- 目标主机由清单（或 OpenAPI 文档）定死，模型只能填 operation 声明过的参数；
- 不跟随重定向：被调用方不能把带着凭据的请求引到别处；
- 凭据在发请求时才从保管库取出，只放进请求，不进返回给模型的文本，也不进异常文案；
- 响应体有上限，读够就停。

限流、熔断、重试、双向 TLS 不在这里做——那是企业 API 网关的事（ADR 0003）。
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx
from xuanpu_spec.call_context import CONTEXT_HEADER

from xuanpu_server.errors import InvalidRequestError, PackageInvalidError, PermissionDeniedError

if TYPE_CHECKING:
    from xuanpu_spec import ApiDocument, Operation
    from xuanpu_spec.manifest import ConnectorSpec, HttpAuth

    from xuanpu_server.runtime.call_context import CallContextSigner
    from xuanpu_server.secrets import SecretVault

MAX_RESPONSE_BYTES = 1024 * 1024
# 凭据头的取值里不能有的字符：放进请求头会断开或改写报文
_HEADER_BREAKERS = ("\r", "\n", "\0")
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/problem+json")


class ConnectorCallError(Exception):
    """连接器调用失败（超时之外的失败）。文案会交给模型，不得包含凭据。"""


def resolve_base_url(spec: ConnectorSpec, document: ApiDocument) -> str:
    """接口的基地址：清单里的优先，否则用文档 ``servers`` 里第一个绝对地址。"""
    candidates = [spec.base_url] if spec.base_url else list(document.servers)
    for candidate in candidates:
        parts = urlsplit(candidate)
        if parts.scheme in ("http", "https") and parts.netloc:
            return candidate.rstrip("/")
    raise PackageInvalidError(f"连接器 {spec.name!r} 没有可用的基地址")


async def auth_header(auth: HttpAuth, *, tenant_id: str, vault: SecretVault) -> tuple[str, str]:
    """按清单声明的方式生成凭据头 ``(头名, 头值)``。密钥现取现用，轮换即时生效。

    HTTP 连接器与远端 MCP 连接器共用这一份写法：``bearer`` 放 ``Authorization: Bearer``，
    ``basic`` 放 ``Authorization: Basic``（密钥取值形如 ``用户:口令``），``header`` 放到
    清单指定的头。``query`` 方式的凭据在查询参数里、不在头里，由调用方自己处理。

    取值里有换行或空字符时拒绝：放进请求头会改写报文。报错里只有密钥名，不回显取值。

    Raises:
        SecretMissingError: 密钥尚未配置。
        InvalidRequestError: 密钥取值里有 CR、LF 或空字符。
        ValueError: ``auth.type`` 是 ``query``。
    """
    if auth.type == "query":
        raise ValueError("query 方式的凭据放在查询参数里，不是请求头")
    secret = await vault.get(tenant_id, auth.secret)
    if any(char in secret for char in _HEADER_BREAKERS):
        raise InvalidRequestError(
            f"密钥 {auth.secret!r} 的取值里有换行或空字符，不能放进请求头；请重新写入这条密钥"
        )
    if auth.type == "bearer":
        return "Authorization", f"Bearer {secret}"
    if auth.type == "basic":
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        return "Authorization", f"Basic {encoded}"
    assert auth.header is not None
    return auth.header, secret


def _scalar(value: Any) -> str:
    """参数值 → 字符串。布尔按 JSON 的写法。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _check_parameters(operation: Operation, args: dict[str, Any]) -> None:
    """拒绝保留头、大小写重复头及未声明参数，避免HTTP语义与模型输入分歧。"""
    if any(parameter.name.lower() == CONTEXT_HEADER.lower() for parameter in operation.parameters):
        raise ValueError("平台签名头不能作为工具参数")
    if any(name.lower() == CONTEXT_HEADER.lower() for name in args):
        raise ValueError("平台签名头不能由模型提供")
    header_names = [p.name.lower() for p in operation.parameters if p.location == "header"]
    if len(set(header_names)) != len(header_names):
        raise ValueError("头参数大小写不敏感，不能重复声明")
    declared = {parameter.name for parameter in operation.parameters}
    allowed = declared | ({"body"} if operation.request_body is not None else set())
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ValueError(f"未声明的参数：{', '.join(unknown)}")


def build_request(
    base_url: str, operation: Operation, args: dict[str, Any]
) -> tuple[str, str, list[tuple[str, str]], dict[str, str], Any]:
    """把工具参数拆到路径、查询、头与请求体，返回 ``(方法, URL, 查询, 头, 请求体)``。

    Raises:
        ValueError: 缺必填参数，或给了 operation 没声明的参数。
    """
    _check_parameters(operation, args)
    path = operation.path
    query: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    for parameter in operation.parameters:
        if parameter.name not in args or args[parameter.name] is None:
            if parameter.required:
                raise ValueError(f"缺少必填参数：{parameter.name}")
            continue
        value = args[parameter.name]
        if parameter.location == "path":
            # 路径参数整体转义，斜杠也转：不允许借参数改写路径层级
            path = path.replace("{" + parameter.name + "}", quote(_scalar(value), safe=""))
        elif parameter.location == "query":
            items = value if isinstance(value, list) else [value]
            query.extend((parameter.name, _scalar(item)) for item in items)
        else:
            headers[parameter.name] = _scalar(value)
    if "{" in path:
        raise ValueError(f"路径里还有未填的参数：{path}")
    body = args.get("body")
    if body is None and operation.request_body_required:
        raise ValueError("缺少请求体 body")
    return operation.method.upper(), base_url + path, query, headers, body


class HttpApiClient:
    """一个 HTTP 连接器的调用端。"""

    def __init__(
        self,
        *,
        base_url: str,
        auth: HttpAuth | None,
        tenant_id: str,
        vault: SecretVault,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        context_audience: str | None = None,
        context_signer: CallContextSigner | None = None,
    ) -> None:
        """
        Args:
            base_url: 接口基地址。
            auth: 凭据注入方式；None 表示不需要凭据。
            tenant_id: 所属租户，取密钥时用。
            vault: 密钥保管库。
            timeout_seconds: 单次请求的超时。
            transport: 自定义传输，测试时用。
            context_audience: 连接器请求的平台身份受众；None 不附带签名。
            context_signer: 部署配置限定可信源和受众的签名器。
        """
        self._context_audience, self._context_signer = context_audience, context_signer
        self._base_url = base_url
        self._auth = auth
        self._tenant_id = tenant_id
        self._vault = vault
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
            # 不读环境里的代理与证书配置：出口走哪里由部署决定，不由进程环境隐式决定
            trust_env=False,
        )

    async def _credentials(self) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """按清单声明的方式准备凭据，返回 ``(头, 查询参数)``。每次调用现取，密钥轮换即时生效。"""
        if self._auth is None:
            return {}, []
        if self._auth.type == "query":
            assert self._auth.name is not None
            secret = await self._vault.get(self._tenant_id, self._auth.secret)
            return {}, [(self._auth.name, secret)]
        name, value = await auth_header(self._auth, tenant_id=self._tenant_id, vault=self._vault)
        return {name: value}, []

    async def _sign_request(self, prepared: httpx.Request, body: Any) -> None:
        """从最终唯一HTTP头和URL签名，不再重建即将发送的请求。"""
        if self._context_audience is None:
            return
        if self._context_signer is None:
            raise PermissionDeniedError("平台未配置调用签名")
        prepared.headers[CONTEXT_HEADER] = await self._context_signer.sign(
            tenant_id=self._tenant_id, audience=self._context_audience,
            url=str(prepared.url), method=prepared.method,
            query=list(prepared.url.params.multi_items()), body=body,
            idempotency_key=prepared.headers.get("Idempotency-Key", ""),
            content_type=prepared.headers.get("Content-Type", ""),
        )

    async def _perform(self, operation: Operation, args: dict[str, Any]) -> tuple[httpx.Response, bytes] | str:
        """调用一个 operation，返回 ``(给模型看的文本, 是否出错)``。

        4xx / 5xx 算出错但不抛异常：状态码与响应体对模型有用。
        网络层失败抛 ``ConnectorCallError``，文案里只有异常类型。
        """
        try:
            method, url, query, headers, body = build_request(self._base_url, operation, args)
        except ValueError as exc:
            return f"invalid_arguments: {exc}"
        auth_headers, auth_query = await self._credentials()
        if {name.lower() for name in headers} & {name.lower() for name in auth_headers}:
            return "invalid_arguments: 工具头参数与平台凭据头冲突"
        outgoing_headers = httpx.Headers({**headers, **auth_headers})
        if CONTEXT_HEADER in outgoing_headers:
            raise PermissionDeniedError("平台签名头不能由连接器凭据覆盖")
        if "Accept" not in outgoing_headers:
            outgoing_headers["Accept"] = "application/json"
        if self._context_audience is not None and "Content-Type" not in outgoing_headers:
            outgoing_headers["Content-Type"] = "application/json"
        # 构造一次真实请求；摘要读取其最终头与 URL，签名后发送同一对象，避免重建差异。
        prepared = self._client.build_request(
            method, url, params=[*query, *auth_query], headers=outgoing_headers,
            json=body if body is not None else None,
        )
        await self._sign_request(prepared, body)
        try:
            response = await self._client.send(prepared, stream=True)
            try:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        break
                return response, bytes(raw)
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            # 异常文本里可能带完整 URL（含查询参数里的凭据），只报类型
            raise ConnectorCallError(f"请求失败：{type(exc).__name__}") from exc

    async def call(self, operation: Operation, args: dict[str, Any]) -> tuple[str, bool]:
        """保留模型工具原有的文本响应契约。"""
        result = await self._perform(operation, args)
        if isinstance(result, str):
            return result, True
        response, raw = result
        return _render(response, raw), response.status_code >= 400

    async def call_structured(self, operation: Operation, args: dict[str, Any]) -> dict[str, Any]:
        """人工辅助入口返回有界结构化结果，不把业务状态改写成操作成功。"""
        result = await self._perform(operation, args)
        if isinstance(result, str):
            raise InvalidRequestError(result)
        response, raw = result
        truncated = len(raw) > MAX_RESPONSE_BYTES
        content_type = response.headers.get("content-type", "").lower()
        body: Any = None
        if raw and not 300 <= response.status_code < 400:
            if content_type.startswith(_TEXT_TYPES) or "json" in content_type:
                body = raw[:MAX_RESPONSE_BYTES].decode(response.encoding or "utf-8", errors="replace")
                if "json" in content_type and not truncated:
                    try:
                        body = json.loads(body)
                    except ValueError:
                        raise ConnectorCallError("响应 JSON 不合法") from None
            else:
                body = {"content_type": content_type, "bytes": min(len(raw), MAX_RESPONSE_BYTES)}
        return {"status": response.status_code, "body": body,
                "is_error": response.status_code >= 400, "truncated": truncated}

    async def close(self) -> None:
        """关闭连接。"""
        await self._client.aclose()


def _compact_json(text: str) -> str:
    """把 JSON 压成一行（省 token）。声称是 JSON 却解析不了的，原样返回——模型能看出问题。"""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False)
    except ValueError:
        return text


def _render(response: httpx.Response, raw: bytes) -> str:
    """把响应整理成给模型看的文本：状态行加正文。"""
    status = f"HTTP {response.status_code}"
    if 300 <= response.status_code < 400:
        return f"{status}（重定向未跟随）"
    content_type = response.headers.get("content-type", "").lower()
    truncated = len(raw) > MAX_RESPONSE_BYTES
    raw = raw[:MAX_RESPONSE_BYTES]
    if not raw:
        return status
    if not content_type.startswith(_TEXT_TYPES) and "json" not in content_type:
        return f"{status}\n[{content_type or '未知类型'}，{len(raw)} 字节，未展示]"
    text = raw.decode(response.encoding or "utf-8", errors="replace")
    if "json" in content_type and not truncated:
        text = _compact_json(text)
    suffix = "\n…[响应体超过上限，已截断]" if truncated else ""
    return f"{status}\n{text}{suffix}"


__all__ = [
    "MAX_RESPONSE_BYTES",
    "ConnectorCallError",
    "HttpApiClient",
    "auth_header",
    "build_request",
    "resolve_base_url",
]
