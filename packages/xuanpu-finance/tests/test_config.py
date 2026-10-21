"""独立服务配置拒绝共享凭据捷径和生产合成解析。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from xuanpu_finance.config import FinanceConfig


def settings(**extra: Any) -> dict[str, Any]:
    """使用路径引用，测试不写入或返回实际密钥。"""
    return {
        "database_dsn_file": "secrets/finance-dsn",
        "service_token_file": "secrets/service-token",
        "issuer": "platform",
        "audience": "finance",
        "public_key_files": {"k1": "secrets/public.pem"},
        "gateway": {
            "base_url": "https://platform.example",
            "tenant_token_files": {"acme": "secrets/api-key"},
        },
        "objects": {"kind": "local", "root": "finance-objects", "key_file": "secrets/object-key"},
        "companies": [],
        "grants": [],
        **extra,
    }


def test_production_cannot_use_synthetic_parser() -> None:
    """合成材料不能混入生产解释为官方凭证。"""
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(settings(synthetic=True))


def test_explicit_test_profile_and_separate_secrets() -> None:
    """测试模式必须显式选择，数据库和对象凭据独立注入。"""
    config = FinanceConfig.model_validate(settings(environment="test", synthetic=True))
    assert config.synthetic is True and config.database_dsn_file.name == "finance-dsn"
    assert config.objects.key_file.name == "object-key"


def test_platform_database_credential_is_not_a_setting() -> None:
    """财务配置不接受平台数据库或平台对象凭据。"""
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(settings(platform_database_dsn="private"))


def test_unknown_module_mode_is_rejected() -> None:
    """开启模式必须来自明确的生命周期集合。"""
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(settings(mode="anything"))
