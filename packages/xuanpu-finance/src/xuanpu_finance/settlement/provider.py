"""使用成熟异步HTTP客户端执行固定请求号和原字节HMAC认证。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.settlement.provider_models import ProviderResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xuanpu_finance.settlement.provider_models import ProviderConfig

MAX_RECEIPT_BYTES = 1024 * 1024
RESULT_SCOPE = (
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复JSON键，避免认证端与结果端采用不同覆盖规则。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("渠道JSON包含重复字段")
        result[key] = value
    return result


class ProviderResponseError(FinanceError):
    """保留拒绝回执原字节供事务存储，禁止据其更改业务终态。"""

    def __init__(self, raw: bytes, code: str = "PROVIDER_RESPONSE_INVALID") -> None:
        """错误信息不包含渠道返回的个人信息或凭据。"""
        super().__init__(code, 502, "渠道回执不满足冻结请求契约")
        self.raw = raw


class ProviderRegistry:
    """按固定provider和配置版本选择可替换通道。"""

    def __init__(
        self,
        configs: list[ProviderConfig],
        client: httpx.AsyncClient,
        *,
        tokens: Mapping[tuple[str, int], str],
        callback_secrets: Mapping[tuple[str, int], str],
    ) -> None:
        """客户端和已读取凭据由运行时管理，通道不隐式读取文件。"""
        self.client, self.tokens, self.secrets = client, dict(tokens), dict(callback_secrets)
        self.configs = {
            (config.provider, config.configuration_version): config for config in configs
        }
        if len(self.configs) != len(configs):
            raise ValueError("provider与configuration_version必须全实例唯一")
        for key in self.configs:
            if not self.tokens.get(key) or not self.secrets.get(key):
                raise ValueError("通道凭据必须显式提供且非空")

    def _config(self, provider: str, version: int) -> ProviderConfig:
        """不允许缺失旧版本时切换到新通道。"""
        config = self.configs.get((provider, version))
        if config is None:
            raise FinanceError("PROVIDER_UNCONFIGURED", 503, "固定版本通道未配置")
        return config

    def _request_config(self, request: dict[str, Any], *, submit: bool) -> ProviderConfig:
        """发送前核对持久环境和版本，不接受调用者提供任意URL。"""
        config = self._config(request["provider"], request["configuration_version"])
        for field in ("tenant_id", "legal_entity_id", "adapter_version", "simulation"):
            if request.get(field) != getattr(config, field):
                raise FinanceError("PROVIDER_SCOPE_MISMATCH", 409, "通道范围或环境不匹配")
        if not config.supports_query or (
            submit and (not config.enabled or not config.supports_idempotency)
        ):
            raise FinanceError("PROVIDER_CAPABILITY_REQUIRED", 409, "通道未启用或缺少幂等查单能力")
        unhashed = {key: value for key, value in request.items() if key != "payload_hash"}
        if request.get("payload_hash") != request_digest(unhashed):
            raise FinanceError("PROVIDER_PAYLOAD_MISMATCH", 409, "固定请求摘要不符")
        return config

    async def submit(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """只发送一次外部提交，网络错误由worker转未知。"""
        return await self._exchange(request, submit=True)

    async def query(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """按同一请求号只读查单，停用通道仍可核对旧工作。"""
        return await self._exchange(request, submit=False)

    async def _exchange(
        self, request: dict[str, Any], *, submit: bool
    ) -> tuple[dict[str, Any], bytes]:
        """有限流式读取原字节，不跟重定向、不按HTTP200推断成功。"""
        config = self._request_config(request, submit=submit)
        key = (config.provider, config.configuration_version)
        headers = {
            "Authorization": f"Bearer {self.tokens[key]}",
            "Idempotency-Key": request["external_request_id"],
        }
        path = (
            "/v1/requests"
            if submit
            else "/v1/requests/" + quote(request["external_request_id"], safe="")
        )
        raw = bytearray()
        try:
            async with self.client.stream(
                "POST" if submit else "GET",
                config.base_url + path,
                headers=headers,
                json=request if submit else None,
                follow_redirects=False,
            ) as response:
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RECEIPT_BYTES:
                        raise ProviderResponseError(
                            bytes(raw[:MAX_RECEIPT_BYTES]), "PROVIDER_RESPONSE_TOO_LARGE"
                        )
                content = bytes(raw)
                if response.status_code not in {200, 201, 202} and not (
                    not submit and response.status_code == 404
                ):
                    raise ProviderResponseError(content, "PROVIDER_HTTP_ERROR")
        except httpx.HTTPError as error:
            if raw:
                raise ProviderResponseError(bytes(raw), "PROVIDER_NETWORK_UNKNOWN") from error
            raise
        return self._validated_result(request, content), content

    def _validated_result(self, request: dict[str, Any], raw: bytes) -> dict[str, Any]:
        """严格匹配每个冻结维度和成功回执的账套期间。"""
        try:
            parsed = ProviderResult.model_validate(
                json.loads(raw, object_pairs_hook=_unique_object)
            )
            result = parsed.model_dump(mode="json", exclude_none=True)
            if any(result[field] != request[field] for field in RESULT_SCOPE):
                raise ValueError("回执范围不符")
            if parsed.action != "execute_payment" and parsed.status == "succeeded":
                if any(
                    result.get(field) != request.get(field)
                    for field in ("ledger_id", "accounting_period")
                ):
                    raise ValueError("账套期间不符")
            return result
        except (ValidationError, ValueError, KeyError, RecursionError) as error:
            raise ProviderResponseError(raw) from error

    def authenticate_callback(
        self,
        provider: str,
        configuration_version: int,
        headers: Mapping[str, str],
        raw: bytes,
    ) -> tuple[str, dict[str, Any]]:
        """先校验时间事件与原字节HMAC，再解析回调，业务层只排查单。"""
        config = self._config(provider, configuration_version)
        lowered = {key.lower(): value for key, value in headers.items()}
        timestamp = lowered.get("x-finance-timestamp", "")
        event = lowered.get("x-finance-event-id", "")
        signature = lowered.get("x-finance-signature", "")
        if (
            len(raw) > MAX_RECEIPT_BYTES
            or not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or abs(time.time() - int(timestamp)) > 300
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event)
            or lowered.get("x-finance-key-id") != config.callback_key_id
        ):
            raise FinanceError("CALLBACK_AUTH_INVALID", 401, "渠道回调认证失败")
        signed = timestamp.encode("ascii") + b"." + event.encode("ascii") + b"." + raw
        secret = self.secrets[(provider, configuration_version)].encode("utf-8")
        expected = "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise FinanceError("CALLBACK_AUTH_INVALID", 401, "渠道回调认证失败")
        return event, self._callback_body(config, raw)

    def _callback_body(self, config: ProviderConfig, raw: bytes) -> dict[str, Any]:
        """认证后的回调也必须属于固定配置，不信任模型或请求体选择通道。"""
        try:
            body: Any = json.loads(raw, object_pairs_hook=_unique_object)
            fields = (
                "tenant_id",
                "legal_entity_id",
                "provider",
                "configuration_version",
                "simulation",
            )
            if (
                not isinstance(body, dict)
                or any(body.get(field) != getattr(config, field) for field in fields)
                or type(body.get("configuration_version")) is not int
                or type(body.get("simulation")) is not bool
            ):
                raise ValueError("回调范围不匹配")
            if (
                not isinstance(body.get("external_request_id"), str)
                or not body["external_request_id"]
            ):
                raise ValueError("回调缺少请求号")
            return body
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise FinanceError("CALLBACK_BODY_INVALID", 422, "渠道回调内容无效") from error
