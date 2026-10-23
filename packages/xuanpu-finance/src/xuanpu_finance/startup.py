"""正式内部实例在资源装配前重新校验配置并捕获一次凭据。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
from cryptography.exceptions import UnsupportedAlgorithm

from xuanpu_finance.config import FinanceConfig, load_config
from xuanpu_finance.errors import startup_blocked
from xuanpu_finance.readiness.credentials import capture_credentials, tls_origin

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.readiness.credentials import CredentialSnapshot


def load_startup_config(path: Path) -> FinanceConfig:
    """只对明确正式内部配置的结构错误归一，配置文件仅解析一次。"""
    return load_config(path, startup=True)


async def prepare_startup(config: FinanceConfig) -> tuple[FinanceConfig, CredentialSnapshot | None]:
    """显式测试或普通实例保持兼容，正式内部实例在首个副作用前拒绝。"""
    guarded = config.profile == "internal_records" and config.environment == "production"
    try:
        validated = FinanceConfig.model_validate(config.model_dump())
        if not guarded:
            return validated, None
        if not tls_origin(validated.gateway.base_url) or (
            validated.objects.kind == "s3" and not tls_origin(validated.objects.endpoint_url or "")
        ):
            raise ValueError("transport_tls_invalid")
        if not {c.tenant_id for c in validated.companies} <= set(validated.gateway.tenant_token_files):
            raise ValueError("gateway_credentials_invalid")
        snapshot = await anyio.to_thread.run_sync(capture_credentials, validated)
        return validated, snapshot
    except (OSError, ValueError, UnicodeError, UnsupportedAlgorithm):
        if not guarded:
            raise
        raise startup_blocked() from None
