"""平台服务的配置。

配置来自一个 YAML 文件。取值里的 ``${NAME}`` 由加载方用它显式传入的变量表展开——
平台代码自己不读环境变量，进程入口（命令行）读了之后传进来。
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path  # noqa: TC003 —— pydantic 在运行时解析字段类型
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SHA256_HEX = r"^[0-9a-f]{64}$"


class ConfigError(ValueError):
    """配置文件缺失、无法解析或取值不合法。"""


class _Section(BaseModel):
    """配置段的基类：拼错的键要报错，不能被悄悄忽略。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ListenConfig(_Section):
    """监听地址。默认只监听本机。"""

    host: str = "127.0.0.1"
    port: int = Field(default=8420, ge=1, le=65535)


class PostgresConfig(_Section):
    """一个 Postgres 连接。"""

    dsn: str
    schema_name: str = Field(default="public", alias="schema")
    pool_min: int = Field(default=1, ge=1)
    pool_max: int = Field(default=10, ge=1)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ControlPlaneConfig(_Section):
    """控制面存储。``memory`` 只用于试用：进程退出即丢失。"""

    backend: Literal["memory", "postgres"] = "memory"
    postgres: PostgresConfig | None = None

    @model_validator(mode="after")
    def _postgres_needs_connection(self) -> ControlPlaneConfig:
        if self.backend == "postgres" and self.postgres is None:
            raise ValueError("backend 为 postgres 时必须给出 postgres 段")
        return self


class StateConfig(_Section):
    """状态面后端：会话主存与线程目录放在哪里。

    Attributes:
        backend: ``local`` 用内核的本机默认实现，会话主存与线程目录都在数据目录下，
            只适合单实例（多实例跨实例接手会话就得共用数据目录）；``postgres`` 把线程
            目录与会话主存都放进库里，每个租户一组带专属前缀的表，多实例不需要共享
            数据目录。早先版本的 ``postgres`` 只是把消息镜像进库，镜像与主存是同一张表，
            升级后直接从库里续接。
        postgres: ``backend`` 为 ``postgres`` 时的连接。
    """

    backend: Literal["local", "postgres"] = "local"
    postgres: PostgresConfig | None = None

    @model_validator(mode="after")
    def _postgres_needs_connection(self) -> StateConfig:
        if self.backend == "postgres" and self.postgres is None:
            raise ValueError("backend 为 postgres 时必须给出 postgres 段")
        return self


class TierConfig(_Section):
    """一个档位对应的模型。"""

    model: str
    timeout_seconds: float = Field(default=300.0, gt=0)


class ModelsConfig(_Section):
    """模型网关。"""

    base_url: str
    api_key: str | None = None
    api_key_secret: str | None = None
    tiers: dict[Literal["fast", "balanced", "deep"], TierConfig]

    @model_validator(mode="after")
    def _complete(self) -> ModelsConfig:
        missing = [tier for tier in ("fast", "balanced", "deep") if tier not in self.tiers]
        if missing:
            raise ValueError(f"tiers 缺少档位：{', '.join(missing)}")
        if (self.api_key is None) == (self.api_key_secret is None):
            raise ValueError("api_key 与 api_key_secret 必须且只能给一个")
        return self


class SandboxConfig(_Section):
    """沙盒后端。"""

    backend: Literal["local", "docker"] = "local"
    image: str = "python:3.12-slim"
    memory_mb: int | None = Field(default=1024, ge=16)
    cpus: float | None = Field(default=1.0, gt=0)
    docker_binary: str = "docker"
    docker_env: dict[str, str] = Field(default_factory=dict)


class LocalObjectStoreConfig(_Section):
    """本机目录形态的对象存储。"""

    root: Path | None = None
    """存放对象的目录；缺省为 ``<data_dir>/objects``。给了必须是绝对路径。"""

    @model_validator(mode="after")
    def _absolute(self) -> LocalObjectStoreConfig:
        if self.root is not None and not self.root.is_absolute():
            raise ValueError("root 必须是绝对路径")
        return self


class S3ObjectStoreConfig(_Section):
    """S3 兼容的对象存储（阿里云 OSS 等）。两个密钥经 ``${VAR}`` 从环境展开，与主密钥同等对待。"""

    endpoint: str = Field(pattern=r"^https?://")
    bucket: str
    region: str
    prefix: str = ""
    """加在所有对象键前面的前缀：为空，或不以 ``/`` 开头、以 ``/`` 结尾。"""
    access_key_id: str
    secret_access_key: str
    addressing_style: Literal["virtual", "path"] = "virtual"

    @model_validator(mode="after")
    def _prefix_shape(self) -> S3ObjectStoreConfig:
        if self.prefix and (self.prefix.startswith("/") or not self.prefix.endswith("/")):
            raise ValueError("prefix 要么为空，要么不以 / 开头、以 / 结尾")
        return self


class ObjectStoreConfig(_Section):
    """对象存储。``local`` 落本地目录，只用于试用与单机；多实例用 ``s3``（阿里云 OSS 等）。"""

    backend: Literal["local", "s3"] = "local"
    local: LocalObjectStoreConfig = LocalObjectStoreConfig()
    s3: S3ObjectStoreConfig | None = None

    @model_validator(mode="after")
    def _s3_needs_section(self) -> ObjectStoreConfig:
        if self.backend == "s3" and self.s3 is None:
            raise ValueError("backend 为 s3 时必须给出 s3 段")
        return self


class LimitsConfig(_Section):
    """平台给每个会话的上限。业务包可以调低，不能调高。"""

    max_session_tokens: int = Field(default=2_000_000, ge=1000)
    max_iterations: int = Field(default=60, ge=1)
    connector_timeout_seconds: float = Field(default=60.0, gt=0)
    session_idle_seconds: float = Field(default=300.0, ge=1)
    runtime_idle_seconds: float = Field(default=900.0, ge=1)


class AuditConfig(_Section):
    """审计选项。"""

    capture_requests: bool = False
    """是否把每次发给模型的完整请求写进审计。内容敏感，默认关闭。"""


class ServerConfig(_Section):
    """平台服务的完整配置。"""

    data_dir: Path
    root_key_hash: str = Field(pattern=_SHA256_HEX)
    master_keys: dict[str, str] = Field(min_length=1)
    primary_master_key: str
    models: ModelsConfig
    listen: ListenConfig = ListenConfig()
    control_plane: ControlPlaneConfig = ControlPlaneConfig()
    state: StateConfig = StateConfig()
    sandbox: SandboxConfig = SandboxConfig()
    object_store: ObjectStoreConfig = ObjectStoreConfig()
    limits: LimitsConfig = LimitsConfig()
    audit: AuditConfig = AuditConfig()
    lease_seconds: float = Field(default=60.0, ge=5)
    shutdown_drain_seconds: float = Field(default=30.0, ge=0)
    instance_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> ServerConfig:
        if not self.data_dir.is_absolute():
            raise ValueError("data_dir 必须是绝对路径")
        if self.primary_master_key not in self.master_keys:
            raise ValueError("primary_master_key 不在 master_keys 里")
        self.decoded_master_keys()
        return self

    def decoded_master_keys(self) -> dict[str, bytes]:
        """主密钥的原始字节。配置里写的是 base64。"""
        decoded: dict[str, bytes] = {}
        for key_id, encoded in self.master_keys.items():
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"主密钥 {key_id!r} 不是合法的 base64") from exc
            if len(raw) != 32:
                raise ValueError(f"主密钥 {key_id!r} 解码后必须是 32 字节")
            decoded[key_id] = raw
        return decoded


def expand(value: Any, variables: Mapping[str, str]) -> Any:
    """递归展开字符串里的 ``${NAME}``。引用了没给的变量要报错，不能悄悄当成空串。"""
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise ConfigError(f"配置里引用了未提供的变量 ${{{name}}}")
            return variables[name]

        return _VARIABLE.sub(_replace, value)
    if isinstance(value, dict):
        return {key: expand(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, variables) for item in value]
    return value


def parse_config(text: str, *, variables: Mapping[str, str]) -> ServerConfig:
    """从 YAML 文本解析配置。

    Raises:
        ConfigError: 不是合法 YAML、引用了未提供的变量，或取值不合法。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置的顶层必须是映射")
    try:
        return ServerConfig.model_validate(expand(data, variables))
    except ValidationError as exc:
        problems = "".join(
            f"\n  - {'.'.join(str(part) for part in item['loc']) or '<根>'}：{item['msg']}"
            for item in exc.errors()
        )
        raise ConfigError(f"配置不合法{problems}") from exc


def load_config(path: Path, *, variables: Mapping[str, str]) -> ServerConfig:
    """从文件读取配置。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读不到配置文件 {path}：{exc.strerror or exc}") from exc
    return parse_config(text, variables=variables)


__all__ = [
    "AuditConfig",
    "ConfigError",
    "ControlPlaneConfig",
    "LimitsConfig",
    "ListenConfig",
    "LocalObjectStoreConfig",
    "ModelsConfig",
    "ObjectStoreConfig",
    "PostgresConfig",
    "S3ObjectStoreConfig",
    "SandboxConfig",
    "ServerConfig",
    "StateConfig",
    "TierConfig",
    "expand",
    "load_config",
    "parse_config",
]
