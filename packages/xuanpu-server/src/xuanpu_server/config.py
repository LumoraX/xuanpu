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
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from xuanpu_server.tenancy import validate_tenant_id

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import ValidationInfo

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SHA256_HEX = r"^[0-9a-f]{64}$"
# 站内路径：以 / 开头，第二个字符不是 / 或 \（否则浏览器会当成别的站点），全程不含 \ 与空白
_SITE_PATH = r"^/([^/\\\s][^\\\s]*)?$"


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
    """一个 Postgres 连接（连接池）。

    Attributes:
        dsn: 连接串。
        schema_name: 表所在的 schema（配置里写 ``schema``）。
        pool_min: 连接池最少保持的连接数。
        pool_max: 连接池最多的连接数。
        connect_timeout_seconds: 新建一条连接的时限，交给 asyncpg 的 ``timeout``。
        command_timeout_seconds: 单条语句的时限，交给 asyncpg 的 ``command_timeout``。
            平台日常的语句都是短查询，60 秒足够宽松，又能让卡住的库表现为报错。注意状态面
            的表是租户第一次使用时在运行中迁移的（``xuanpu migrate`` 只管控制面），以后若有
            大表上的迁移，要临时调大状态面这一项。

    asyncpg 没有连接池级别的「等空闲连接」时限：池满时取连接会一直排队，所以 ``pool_max``
    要按并发量留足。
    """

    dsn: str
    schema_name: str = Field(default="public", alias="schema")
    pool_min: int = Field(default=1, ge=1)
    pool_max: int = Field(default=10, ge=1)
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    command_timeout_seconds: float = Field(default=60.0, gt=0)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ControlPlaneConfig(_Section):
    """控制面存储。``memory`` 只用于试用：进程退出即丢失。"""

    backend: Literal["memory", "postgres"] = "memory"
    postgres: PostgresConfig | None = None

    @model_validator(mode="after")
    def _postgres_needs_connection(self) -> ControlPlaneConfig:
        """``postgres`` 后端必须给出连接段。"""
        if self.backend == "postgres" and self.postgres is None:
            raise ValueError("backend 为 postgres 时必须给出 postgres 段")
        return self


class StateConfig(_Section):
    """状态面后端：会话主存与线程目录放在哪里。

    Attributes:
        backend: ``local`` 用内核的本机默认实现，会话主存与线程目录都在数据目录下，
            只适合单实例；多实例用 ``postgres``：线程目录与会话主存都放进库里，每个租户
            一组带专属前缀的表，各实例各用各的数据目录。早先版本的 ``postgres`` 只是把
            消息镜像进库：镜像是尽力写入，可能不完整。升级前若有活动会话，以节点本地的
            JSONL 为准，升级后这些会话可能无法续接。
        postgres: ``backend`` 为 ``postgres`` 时的连接。
    """

    backend: Literal["local", "postgres"] = "local"
    postgres: PostgresConfig | None = None

    @model_validator(mode="after")
    def _postgres_needs_connection(self) -> StateConfig:
        """``postgres`` 后端必须给出连接段。"""
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
        """三个档位都要配；``api_key`` 与 ``api_key_secret`` 必须且只能给一个。"""
        missing = [tier for tier in ("fast", "balanced", "deep") if tier not in self.tiers]
        if missing:
            raise ValueError(f"tiers 缺少档位：{', '.join(missing)}")
        if (self.api_key is None) == (self.api_key_secret is None):
            raise ValueError("api_key 与 api_key_secret 必须且只能给一个")
        return self


class SandboxConfig(_Section):
    """沙盒后端。

    stdio 连接器进程跑在每个运行时单独的连接器沙盒里（只读业务包目录、只写连接器工作目录、
    可出网），与脚本的沙盒分开。

    Attributes:
        connector_readable_roots: ``local`` 后端下连接器沙盒里额外可读的目录（绝对路径），
            用来放开连接器自己的解释器与依赖所在的目录；受限读策略下系统运行时目录之外
            的地方默认都读不到。只用于 ``local`` 后端。不能是根目录，也不能与数据目录、
            本机对象存储目录相同或互为上下级——这些要解析符号链接、看目录是否存在，在
            装配平台时检查（``bootstrap``）。
        connector_image: ``docker`` 后端下连接器容器用的镜像；不给则与 ``image`` 相同。
            镜像里要装好连接器需要的运行时。只用于 ``docker`` 后端。

    后端对不上的连接器选项（``docker`` 后端配了 ``connector_readable_roots``、``local``
    后端配了 ``connector_image``）直接报错，不悄悄忽略。
    """

    backend: Literal["local", "docker"] = "local"
    image: str = "python:3.12-slim"
    memory_mb: int | None = Field(default=1024, ge=16)
    cpus: float | None = Field(default=1.0, gt=0)
    docker_binary: str = "docker"
    docker_env: dict[str, str] = Field(default_factory=dict)
    connector_readable_roots: tuple[Path, ...] = ()
    connector_image: str | None = None

    @field_validator("connector_image")
    @classmethod
    def _image_not_blank(cls, value: str | None) -> str | None:
        """去掉首尾空白；空白的镜像名拒绝。"""
        if value is None:
            return None
        if not value.strip():
            raise ValueError("connector_image 不能为空白；不给则与 image 相同")
        return value.strip()

    @model_validator(mode="after")
    def _connector_options(self) -> SandboxConfig:
        """连接器选项：可读目录须为绝对路径；后端对不上的选项报错，不悄悄忽略。"""
        relative = [str(root) for root in self.connector_readable_roots if not root.is_absolute()]
        if relative:
            raise ValueError(f"connector_readable_roots 必须是绝对路径：{', '.join(relative)}")
        if self.backend == "docker" and self.connector_readable_roots:
            raise ValueError(
                "connector_readable_roots 只用于 local 后端；docker 后端下连接器的运行时"
                "装在 connector_image 里"
            )
        if self.backend == "local" and self.connector_image is not None:
            raise ValueError(
                "connector_image 只用于 docker 后端；local 后端用 connector_readable_roots "
                "放开连接器的运行时"
            )
        return self


class LocalObjectStoreConfig(_Section):
    """本机目录形态的对象存储。"""

    root: Path | None = None
    """存放对象的目录；缺省为 ``<data_dir>/objects``。给了必须是绝对路径。"""

    @model_validator(mode="after")
    def _absolute(self) -> LocalObjectStoreConfig:
        """给了 ``root`` 就必须是绝对路径。"""
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
        """``prefix`` 要么为空，要么不以 ``/`` 开头、以 ``/`` 结尾。"""
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
        """``s3`` 后端必须给出 ``s3`` 段。"""
        if self.backend == "s3" and self.s3 is None:
            raise ValueError("backend 为 s3 时必须给出 s3 段")
        return self


class LimitsConfig(_Section):
    """平台给每个会话的上限。业务包可以调低，不能调高。"""

    max_session_tokens: int = Field(default=2_000_000, ge=1000)
    max_iterations: int = Field(default=60, ge=1)
    connector_timeout_seconds: float = Field(default=60.0, gt=0)
    connector_startup_seconds: float = Field(default=30.0, gt=0)
    """启动一个 stdio 连接器（起进程、握手、列出工具）的时限。"""
    session_idle_seconds: float = Field(default=300.0, ge=1)
    runtime_idle_seconds: float = Field(default=900.0, ge=1)


class AuditConfig(_Section):
    """审计选项。"""

    capture_requests: bool = False
    """是否把每次发给模型的完整请求写进审计。内容敏感，默认关闭。"""


class MetricsConfig(_Section):
    """指标端点（``GET /metrics``）。"""

    token_hash: str | None = Field(default=None, pattern=_SHA256_HEX)
    """抓取令牌的摘要，``xuanpu keygen metrics`` 生成。不给就不开指标端点（404）。"""


def normalize_origin(value: str, *, name: str) -> str:
    """把只写了源的地址规整成浏览器 ``Origin`` 头的写法：``scheme://host[:port]``。

    协议与主机名转小写、去掉默认端口（http 的 80、https 的 443）与末尾的 ``/``。只收
    ``http://`` 或 ``https://``，不能带用户名密码、路径、查询串或片段；主机名要写 ASCII（国际化
    域名写成 punycode，与浏览器发出的一致）。

    Raises:
        ValueError: 不合要求；``name`` 用在提示里。
    """
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{name} 必须是 http:// 或 https:// 开头的地址，得到 {value!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} 不能带用户名或密码")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(
            f"{name} 只写协议、主机与端口（如 https://xuanpu.example.com），不带路径、查询串或"
            f"片段，得到 {value!r}"
        )
    host = parts.hostname
    if not host.isascii():
        raise ValueError(f"{name} 的主机名要写 ASCII（国际化域名写成 punycode），得到 {value!r}")
    port = parts.port  # 端口不是合法数字时这里报 ValueError
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if parts.scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parts.scheme}://{host}{suffix}"


class IdentityConfig(_Section):
    """员工身份（飞书登录）的平台级设置。租户自己的飞书应用经接口配置，不在这里。

    Attributes:
        session_ttl_seconds: 登录会话的默认有效期（秒）；租户的飞书配置可以覆盖。
        cookie_secure: 登录 cookie 是否只走 HTTPS；本地用 HTTP 开发时设为 false。
        reconcile_interval_seconds: 按飞书通讯录对账的周期（秒）。
        default_tenant: 登录链接省略 ``tenant`` 时用的租户。
        login_landing_path: 登录完成或失败后的默认落地页，站内路径：以 ``/`` 开头、不以
            ``//`` 或 ``/\\`` 开头，不含反斜杠与空白（否则可能被浏览器当成别的站点）。
        feishu_open_base: 飞书开放接口的基地址；测试时指向模拟服务端，也可用于接 Lark 国际版。
            与 ``public_url`` 一样只写源。
        feishu_accounts_base: 飞书授权页的基地址，同上。
    """

    session_ttl_seconds: int = Field(default=604800, ge=60)
    cookie_secure: bool = True
    reconcile_interval_seconds: float = Field(default=86400.0, ge=60)
    default_tenant: str | None = None
    login_landing_path: str = Field(default="/console/", pattern=_SITE_PATH)
    feishu_open_base: str = "https://open.feishu.cn"
    feishu_accounts_base: str = "https://accounts.feishu.cn"

    @field_validator("default_tenant")
    @classmethod
    def _tenant_id_shape(cls, value: str | None) -> str | None:
        """租户标识的字符集与平台其余各处一致。"""
        return validate_tenant_id(value) if value is not None else None

    @field_validator("feishu_open_base", "feishu_accounts_base")
    @classmethod
    def _origin_only(cls, value: str, info: ValidationInfo) -> str:
        """飞书的两个基地址只写源，规整后保存（拼接口路径时不会多出或少了 ``/``）。"""
        return normalize_origin(value, name=str(info.field_name))


class ServerConfig(_Section):
    """平台服务的完整配置。

    ``public_url`` 是平台对外的地址（浏览器与飞书访问平台用的那个），只写源：
    ``https://xuanpu.example.com`` 或带端口的 ``http://127.0.0.1:8420``，不带路径——平台的
    接口与控制台都挂在根路径下（``/v1/…``、``/console/``），飞书的回调地址就是
    ``<public_url>/v1/auth/feishu/callback``。加载时规整为浏览器 ``Origin`` 头的写法（见
    ``normalize_origin``），用来校验 cookie 认证的写请求来自平台自己的页面。没有配置时，cookie
    认证的写请求只查 ``X-Requested-By`` 头、不查 ``Origin``（见 ``identity.cookies``）；启用
    飞书登录时必须配置。
    """

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
    metrics: MetricsConfig = MetricsConfig()
    public_url: str | None = None
    identity: IdentityConfig = IdentityConfig()
    lease_seconds: float = Field(default=60.0, ge=5)
    shutdown_drain_seconds: float = Field(default=30.0, ge=0)
    instance_id: str | None = None

    @field_validator("public_url")
    @classmethod
    def _public_origin(cls, value: str | None) -> str | None:
        """对外地址只写源，规整后保存（规则见类说明）。"""
        return normalize_origin(value, name="public_url") if value is not None else None

    @model_validator(mode="after")
    def _check(self) -> ServerConfig:
        """数据目录是绝对路径；主用主密钥在 ``master_keys`` 里；主密钥都能解码成 32 字节。"""
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
    "IdentityConfig",
    "LimitsConfig",
    "ListenConfig",
    "LocalObjectStoreConfig",
    "MetricsConfig",
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
    "normalize_origin",
    "parse_config",
]
