"""仅从冻结渠道和请求号获取真实证据文件。"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.settlement.provider_models import ProviderEvidence

if TYPE_CHECKING:
    from xuanpu_finance.settlement.provider import ProviderRegistry

MAX_EVIDENCE_BYTES = 20*1024*1024


class EvidenceFetcher:
    """不跟随渠道object_ref或URL，避免凭据泄漏和任意目标访问。"""

    def __init__(self, registries: dict[str, ProviderRegistry]) -> None:
        """注入各类渠道的固定版本注册表。"""
        self.registries = registries

    async def fetch(self, source: dict[str, Any]) -> bytes:
        """主动查固定证据端点，核对实际媒体类型、大小和摘要。"""
        registry = self.registries.get(source["provider_kind"])
        if registry is None:
            raise FinanceError("EVIDENCE_PROVIDER_MISSING", 409, "证据通道未配置")
        request = source["provider_request"]
        config = registry._request_config(request, submit=False)
        evidence = ProviderEvidence.model_validate(source["evidence"])
        path = "/v1/requests/" + quote(request["external_request_id"], safe="")
        path += "/evidence/" + quote(evidence.id, safe="")
        key = (config.provider, config.configuration_version)
        raw = bytearray()
        async with registry.client.stream("GET", config.base_url + path, headers={
            "Authorization": "Bearer " + registry.tokens[key]}, follow_redirects=False) as response:
            if response.status_code >= 500 or response.status_code == 429:
                raise FinanceError("EVIDENCE_TEMPORARILY_UNAVAILABLE", 503, "证据渠道暂不可用")
            if response.status_code != 200:
                raise FinanceError("EVIDENCE_UNAVAILABLE", 409, "所需证据文件无法取得")
            media = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if media != evidence.media_type:
                raise FinanceError("EVIDENCE_MEDIA_MISMATCH", 409, "证据媒体类型不符")
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_EVIDENCE_BYTES:
                    raise FinanceError("EVIDENCE_TOO_LARGE", 413, "证据文件超过20MiB")
        if not raw or hashlib.sha256(raw).hexdigest() != evidence.sha256:
            raise FinanceError("EVIDENCE_HASH_MISMATCH", 409, "证据文件摘要不符")
        return bytes(raw)
