"""独立财务服务的显式配置和密钥文件引用。"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — Pydantic 运行时解析配置类型
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictConfig(BaseModel):
    """拒绝配置拼写错误与未知凭据字段。"""

    model_config = ConfigDict(extra="forbid")


class GatewayConfig(StrictConfig):
    """只通过平台受限 API 访问身份与文件。"""

    base_url: str
    tenant_token_files: dict[str, Path]

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        """目标地址不能带用户名、密码、查询参数或片段。"""
        from urllib.parse import urlsplit

        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("平台网关必须是明确的 HTTP(S) origin")
        return self


class ObjectsConfig(StrictConfig):
    """原件存储使用自己的根、桶和凭据。"""

    kind: Literal["local", "s3"]
    key_file: Path
    root: Path | None = None
    bucket: str | None = None
    endpoint_url: str | None = None
    access_key_file: Path | None = None
    secret_key_file: Path | None = None

    @model_validator(mode="after")
    def validate_storage(self) -> Self:
        """每种后端显式声明必要资源，不能默读宿主云凭据。"""
        if self.kind == "local":
            if self.root is None:
                raise ValueError("本地财务存储必须有独立根目录")
            if any(
                value is not None
                for value in (
                    self.bucket,
                    self.endpoint_url,
                    self.access_key_file,
                    self.secret_key_file,
                )
            ):
                raise ValueError("本地存储不能混用 S3 凭据")
        elif (
            self.root is not None
            or not self.bucket
            or not self.endpoint_url
            or self.access_key_file is None
            or self.secret_key_file is None
        ):
            raise ValueError("S3 财务存储必须显式配置端点、桶和独立凭据")
        return self


class CompanyConfig(StrictConfig):
    """财务实例服务的明确公司列表。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    legal_entity_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    tax_id: str = Field(min_length=1, max_length=64)


class GrantConfig(StrictConfig):
    """公司内业务权限，不继承平台管理员权限。"""

    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    legal_entity_id: str = Field(min_length=1, max_length=128)
    role: Literal["collector", "reviewer"]


class FinanceConfig(StrictConfig):
    """不含平台库地址、对象密钥或环境变量读取逻辑。"""

    database_dsn_file: Path
    service_token_file: Path
    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    public_key_files: dict[str, Path] = Field(min_length=1)
    gateway: GatewayConfig
    objects: ObjectsConfig
    companies: list[CompanyConfig]
    grants: list[GrantConfig]
    mode: Literal["enabled", "draining", "read_only", "disabled"] = "enabled"
    environment: Literal["production", "development", "test"] = "production"
    synthetic: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8443, ge=1, le=65535)

    @model_validator(mode="after")
    def production_guards(self) -> Self:
        """合成格式明确隔离，授权必须指向已配置公司。"""
        if self.environment == "production" and self.synthetic:
            raise ValueError("生产环境不能启用 synthetic parser")
        companies = {(item.tenant_id, item.legal_entity_id) for item in self.companies}
        if len(companies) != len(self.companies):
            raise ValueError("公司配置不能重复")
        grants = {(item.tenant_id, item.user_id, item.legal_entity_id) for item in self.grants}
        if len(grants) != len(self.grants):
            raise ValueError("公司授权不能重复")
        for grant in self.grants:
            if (grant.tenant_id, grant.legal_entity_id) not in companies:
                raise ValueError("授权必须指向已配置公司")
        return self


def load_config(path: Path) -> FinanceConfig:
    """在进入事件循环前读取受信操作员配置；相对路径以配置所在目录为根。"""
    config = FinanceConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    base = path.resolve().parent

    def resolve(value: Path) -> Path:
        """解析配置中的固定路径，不接收员工输入。"""
        return value if value.is_absolute() else base / value

    config.database_dsn_file = resolve(config.database_dsn_file)
    config.service_token_file = resolve(config.service_token_file)
    config.public_key_files = {
        kid: resolve(value) for kid, value in config.public_key_files.items()
    }
    config.gateway.tenant_token_files = {
        tenant: resolve(value) for tenant, value in config.gateway.tenant_token_files.items()
    }
    for name in ("root", "key_file", "access_key_file", "secret_key_file"):
        value = getattr(config.objects, name)
        if value is not None:
            setattr(config.objects, name, resolve(value))
    return config
