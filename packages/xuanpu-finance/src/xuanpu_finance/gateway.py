"""通过通用平台网关读取实际员工状态及固定文件版本。"""

from __future__ import annotations

import hashlib
from email.message import Message
from email.utils import collapse_rfc2231_value
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx

from xuanpu_finance.errors import FinanceError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xuanpu_finance.models import FileRef, FinancePrincipal


class PlatformGateway:
    """财务持有平台受限 API key，不取得平台数据库或对象存储凭据。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        tenant_tokens: Mapping[str, str],
        max_file_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        """固定客户端地址和每租户服务凭据，拒绝任意目标地址。"""
        self.client, self.tenant_tokens = client, dict(tenant_tokens)
        self.max_file_bytes = max_file_bytes

    def _headers(self, who: FinancePrincipal) -> dict[str, str]:
        """只从已认证主体构造代理身份。"""
        token = self.tenant_tokens.get(who.tenant_id)
        if not token:
            raise FinanceError("gateway_unconfigured", 503, "该租户未配置身份和文件通道")
        return {"Authorization": f"Bearer {token}", "X-Xuanpu-Act-As": who.user_id}

    async def active(self, who: FinancePrincipal) -> bool:
        """实际在线复核；网络或畸形响应不能当成授权成功。"""
        try:
            response = await self.client.get(
                "/v1/integration-gateway/identity", headers=self._headers(who)
            )
            if response.status_code in (401, 403, 404):
                return False
            response.raise_for_status()
            data = response.json()
            if (
                not isinstance(data, dict)
                or data.get("tenant_id") != who.tenant_id
                or data.get("user_id") != who.user_id
                or type(data.get("active")) is not bool
            ):
                raise ValueError("身份核对结果不匹配")
            return data["active"] is True
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise FinanceError("identity_unavailable", 503, "在线身份核对暂不可用") from error

    async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
        """流式限量读取本人固定版本，核对完整字节后返回。"""
        try:
            async with self.client.stream(
                "GET",
                f"/v1/integration-gateway/files/{quote(file.file_id, safe='')}",
                params={"revision": file.revision, "sha256": file.sha256},
                headers=self._headers(who),
            ) as response:
                if response.status_code in (401, 403, 404):
                    raise FinanceError("source_unavailable", 404, "原件不存在或没有读取权限")
                response.raise_for_status()
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > self.max_file_bytes:
                        raise FinanceError("file_too_large", 413, "原件超过单文件大小限制")
                data = bytes(raw)
                if hashlib.sha256(data).hexdigest() != file.sha256:
                    raise FinanceError("source_checksum_mismatch", 422, "原件版本摘要不一致")
                disposition = Message()
                disposition["content-disposition"] = response.headers.get("content-disposition", "")
                # 平台同时提供ASCII占位名和RFC2231原名，优先保留扩展原名。
                extended = [value for field, value in disposition.get_params(header="content-disposition", failobj=[])
                            if field == "filename" and isinstance(value, tuple)]
                name = (collapse_rfc2231_value(extended[-1]) if extended else disposition.get_filename()) or file.file_id
                return data, response.headers.get("content-type", "application/octet-stream"), name
        except httpx.HTTPError as error:
            raise FinanceError("file_gateway_unavailable", 503, "原件读取通道暂不可用") from error
