"""业务包清单（``xuanpu.yaml``）的数据模型。

业务方只描述「要什么」，不描述「怎么做」（ADR 0001）：

- 模型只选档位，不写模型名、不写密钥——具体用哪个模型由平台按租户配置换算；
- 审批只选强度，不写规则——规则由平台按强度与工具的副作用分类生成；
- 连接器里的凭据只写引用，明文由平台在调用出口处注入（ADR 0004 决策 5）。

字段一律禁止多余的键：拼错的字段名应当在校验时报错，而不是被悄悄忽略。
"""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

API_VERSION = "xuanpu/v1"
MANIFEST_FILENAME = "xuanpu.yaml"

# 包名、连接器名、密钥名：小写字母、数字、连字符，字母开头
_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
# 语义化版本（不含构建元数据）
_VERSION_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)
# skill 与工具的标识符
_IDENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
# 环境变量名
_ENV_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
# 动态链接器读取的变量前缀（Linux 的 LD_*、macOS 的 DYLD_*），不分大小写一律拒绝。
# 纵深防御：这类变量可能影响沙盒外的启动程序（视后端而定）
_LOADER_ENV_PREFIXES = ("LD_", "DYLD_")
# 包内相对路径：不以 / 开头，逐段检查在校验器里做
_PATH_PATTERN = r"^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,255}$"

Name = Annotated[str, StringConstraints(pattern=_NAME_PATTERN)]
Version = Annotated[str, StringConstraints(pattern=_VERSION_PATTERN)]
Identifier = Annotated[str, StringConstraints(pattern=_IDENT_PATTERN)]
EnvName = Annotated[str, StringConstraints(pattern=_ENV_PATTERN)]
RelativePath = Annotated[str, StringConstraints(pattern=_PATH_PATTERN)]

ModelTier = Literal["fast", "balanced", "deep"]
ApprovalLevel = Literal["strict", "standard", "relaxed"]
Effect = Literal["read", "write", "external"]


class _Strict(BaseModel):
    """所有清单模型的基类：禁止多余字段，实例不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Metadata(_Strict):
    """包的身份信息。"""

    name: Name
    version: Version
    description: Annotated[str, StringConstraints(max_length=500)] = ""


class SkillsSpec(_Strict):
    """skill 目录与对外入口。

    Attributes:
        dir: skill 目录在包内的相对路径。
        entries: 可以作为会话入口的 skill。不在此列的 skill 只能被其他 skill 调用。
    """

    dir: RelativePath = "skills"
    entries: Annotated[tuple[Identifier, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _entries_unique(self) -> SkillsSpec:
        if len(set(self.entries)) != len(self.entries):
            raise ValueError("entries 里有重复的 skill")
        return self


class ModelSpec(_Strict):
    """模型档位。"""

    tier: ModelTier = "balanced"


class BudgetSpec(_Strict):
    """单个会话的预算。

    Attributes:
        max_session_tokens: 会话累计 token 上限；None 用平台默认。
        max_iterations: 单轮对话里模型与工具往返的次数上限；None 用平台默认。
    """

    max_session_tokens: Annotated[int | None, Field(alias="maxSessionTokens", ge=1000)] = None
    max_iterations: Annotated[int | None, Field(alias="maxIterations", ge=1, le=500)] = None


class SandboxSpec(_Strict):
    """脚本与命令的执行环境需求。

    业务方声明需要什么，平台决定用哪种隔离后端来满足。

    Attributes:
        network: 脚本是否需要出网。默认不需要。
        shell: 是否向模型开放在沙盒里执行后台命令的工具。默认不开放。
    """

    network: bool = False
    shell: bool = False


class PolicySpec(_Strict):
    """策略：审批强度、预算、沙盒需求。"""

    approval: ApprovalLevel = "standard"
    budget: BudgetSpec = BudgetSpec()
    sandbox: SandboxSpec = SandboxSpec()


class EnvValue(_Strict):
    """连接器进程的一个环境变量：要么是字面量，要么是密钥引用。"""

    value: str | None = None
    secret: Name | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> EnvValue:
        if (self.value is None) == (self.secret is None):
            raise ValueError("value 与 secret 必须且只能给一个")
        return self


class ConnectorTool(_Strict):
    """连接器里单个工具的声明。"""

    name: Identifier
    effect: Effect | None = None


HttpAuthType = Literal["bearer", "header", "query", "basic"]

# auth.header 不能用的头名（不分大小写）：由 HTTP 协议本身、连接管理或 MCP 传输使用的头。
# 凭据写进这些头会改写请求的含义（目标主机、报文长度、会话），而不是多带一条凭据
_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-type",
        "content-length",
        "accept",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
    }
)


class HttpAuth(_Strict):
    """HTTP 连接器的凭据注入方式。取值一律来自密钥保管库，清单里只写名字。

    Attributes:
        type: ``bearer`` 放 ``Authorization: Bearer <密钥>``；``header`` 放到 ``header`` 指定的头；
            ``query`` 放到 ``name`` 指定的查询参数；``basic`` 的密钥取值形如 ``用户:口令``。
        secret: 密钥名。
        header: ``type=header`` 时的头名。不能是 ``Authorization``（用 ``bearer``），也不能是
            协议、连接管理或 MCP 传输自己用的头（``Host``、``Content-Type``、``Mcp-Session-Id``
            等，见 ``_RESERVED_HEADERS``），不分大小写。
        name: ``type=query`` 时的参数名。
    """

    type: HttpAuthType
    secret: Name
    header: Annotated[str | None, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,64}$")] = None
    name: Annotated[str | None, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")] = None

    @model_validator(mode="after")
    def _shape(self) -> HttpAuth:
        if self.type == "header" and not self.header:
            raise ValueError("type 为 header 时必须给 header")
        if self.type == "query" and not self.name:
            raise ValueError("type 为 query 时必须给 name")
        if self.type == "header" and self.header and self.header.lower() == "authorization":
            raise ValueError("Authorization 头请用 type: bearer")
        if self.type == "header" and self.header and self.header.lower() in _RESERVED_HEADERS:
            raise ValueError(f"header 不能是协议、连接管理或 MCP 传输自己用的头：{self.header}")
        return self


Transport = Literal["stdio", "http", "remote"]

# remote 连接器的凭据只能放在请求头里：query 会把明文写进 URL（URL 常被日志、异常文本带出去），
# basic 暂不开放（需要时可以向后兼容地加上）
_REMOTE_AUTH_TYPES = ("bearer", "header")


def _check_remote_url(url: str | None) -> None:
    """remote 连接器的地址：必填，带主机名的 ``http://`` / ``https://``，端口是数字，不带用户名
    与口令，没有空白与控制字符。"""
    if url is None:
        raise ValueError("remote 连接器必须给 url")
    if any(char.isspace() or not char.isprintable() for char in url):
        raise ValueError("url 里不能有空白或控制字符")
    try:
        parts = urlsplit(url)
        # 端口在取用时才解析：不是数字或超出范围都在这里报出来
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"url 不是合法的地址：{exc}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url 必须是带主机名、以 http:// 或 https:// 开头的地址")
    if "@" in parts.netloc:
        raise ValueError("url 里不能带用户名或口令：凭据请写进 auth，只写密钥名")


class ConnectorSpec(_Strict):
    """一个连接器：业务方接入外部能力的通道。

    三种传输：

    - ``stdio``：由平台启动的 MCP server 进程。模型看到的工具名是 ``mcp__<连接器名>__<工具名>``。
    - ``http``：按 OpenAPI 文档直接调用的 HTTP 接口，每个 operation 是一个工具，
      工具名是 ``api__<连接器名>__<operationId>``。
    - ``remote``：已经部署在别处、经 MCP Streamable HTTP 访问的 MCP server。工具名与
      ``stdio`` 一样是 ``mcp__<连接器名>__<工具名>``。

    Attributes:
        name: 连接器名。
        transport: 传输方式。
        command: ``stdio`` 传输下启动 MCP server 的命令。
        env: 传给 MCP server 进程的环境变量。
        openapi: ``http`` 传输下 OpenAPI 文档在包内的相对路径。
        base_url: ``http`` 传输下接口的基地址；不给则用文档 ``servers`` 里的第一个。
        url: ``remote`` 传输下 MCP server 的端点地址；正式使用应为 ``https://``，
            试用可以是 ``http://``。不能带用户名与口令。
        auth: ``http`` 与 ``remote`` 传输下的凭据注入方式；不给表示不需要凭据。
            ``remote`` 只支持放在请求头里的 ``bearer`` 与 ``header``。
        timeout_seconds: ``http`` 与 ``remote`` 传输下单次调用的超时。
        effect: 该连接器工具的默认副作用分类；默认按最保守的 ``external``。
            ``http`` 传输下 GET / HEAD 的 operation 默认是 ``read``。
        tools: 工具白名单与逐工具的副作用分类（``http`` 传输下按 operationId）。
            留空表示开放全部。
    """

    name: Name
    transport: Transport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[EnvName, EnvValue] = Field(default_factory=dict)
    openapi: RelativePath | None = None
    base_url: Annotated[str | None, Field(alias="baseUrl")] = None
    url: str | None = None
    auth: HttpAuth | None = None
    timeout_seconds: Annotated[float, Field(alias="timeoutSeconds", gt=0, le=600)] = 30.0
    effect: Effect = "external"
    tools: tuple[ConnectorTool, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ConnectorSpec:
        """各传输方式的必填字段齐全，且没有写进别的传输方式的字段。"""
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tools 里有重复的工具名")
        if self.url is not None and self.transport != "remote":
            raise ValueError("url 只用于 remote 连接器")
        if self.transport == "stdio":
            self._check_stdio()
        elif self.transport == "http":
            self._check_http()
        else:
            self._check_remote()
        return self

    def _check_stdio(self) -> None:
        """stdio 连接器：必须有 command；凭据经 env 的 secret 注入，不用 auth。"""
        if not self.command:
            raise ValueError("stdio 连接器必须给 command")
        if self.openapi or self.base_url:
            raise ValueError("openapi / baseUrl 只用于 http 连接器")
        if self.auth:
            raise ValueError("auth 只用于 http 与 remote 连接器；stdio 连接器的凭据写在 env 里")

    def _check_http(self) -> None:
        """http 连接器：必须有 openapi；baseUrl 若给出须是 http(s) 地址。"""
        if not self.openapi:
            raise ValueError("http 连接器必须给 openapi")
        if self.command or self.env:
            raise ValueError("command / env 只用于 stdio 连接器")
        if self.base_url is not None and not self.base_url.startswith(("http://", "https://")):
            raise ValueError("baseUrl 必须以 http:// 或 https:// 开头")

    def _check_remote(self) -> None:
        """remote 连接器：必须有合法的 url；凭据只能放在请求头里。"""
        _check_remote_url(self.url)
        if self.command or self.env:
            raise ValueError("command / env 只用于 stdio 连接器")
        if self.openapi or self.base_url:
            raise ValueError("openapi / baseUrl 只用于 http 连接器")
        if self.auth is not None and self.auth.type not in _REMOTE_AUTH_TYPES:
            raise ValueError(
                f"remote 连接器的 auth.type 只支持 bearer 与 header，不支持 {self.auth.type}"
            )

    @model_validator(mode="after")
    def _no_loader_variables(self) -> ConnectorSpec:
        """拒绝动态链接器变量（纵深防御）：它们可能影响沙盒外的启动程序（视后端而定）。"""
        blocked = sorted(name for name in self.env if name.upper().startswith(_LOADER_ENV_PREFIXES))
        if blocked:
            raise ValueError(
                f"env 不能设置动态链接器变量（LD_*、DYLD_*，不分大小写）：{', '.join(blocked)}。"
                "连接器进程由沙盒外的启动器拉起，这类变量会让启动器在沙盒之外加载任意代码"
            )
        return self

    @property
    def tool_prefix(self) -> str:
        """模型看到的工具名前缀。"""
        # stdio 与 remote 都是 MCP server，前缀相同；只有按 OpenAPI 调用的 http 是 api
        return f"{'api' if self.transport == 'http' else 'mcp'}__{self.name}__"

    def tool_name(self, tool: str) -> str:
        """模型看到的完整工具名。"""
        return self.tool_prefix + tool

    def effect_of(self, tool_name: str, *, read_only: bool = False) -> Effect:
        """某个工具的副作用分类。

        逐工具声明优先；HTTP 只读 operation 默认 ``read``；否则用连接器默认值。
        """
        for tool in self.tools:
            if tool.name == tool_name and tool.effect is not None:
                return tool.effect
        if read_only:
            return "read"
        return self.effect

    def allows(self, tool_name: str) -> bool:
        """该工具是否在白名单内（未声明白名单则全部放行）。"""
        return not self.tools or any(tool.name == tool_name for tool in self.tools)

    @property
    def secret_names(self) -> tuple[str, ...]:
        """该连接器引用的全部密钥名。"""
        found = {item.secret for item in self.env.values() if item.secret}
        if self.auth is not None:
            found.add(self.auth.secret)
        return tuple(sorted(found))


class EvalExpectation(_Strict):
    """评测用例的期望。

    Attributes:
        contains: 最终回复必须包含的片段。
        not_contains: 最终回复不得包含的片段。
        max_tokens: 本轮消耗的 token 上限。
    """

    contains: tuple[str, ...] = ()
    not_contains: Annotated[tuple[str, ...], Field(alias="notContains")] = ()
    max_tokens: Annotated[int | None, Field(alias="maxTokens", ge=1)] = None


class EvalCase(_Strict):
    """一个评测用例：给一个入口 skill 一句输入，检查回复。"""

    name: Name
    entry: Identifier
    input: Annotated[str, StringConstraints(min_length=1)]
    expect: EvalExpectation = EvalExpectation()


class PackageSpec(_Strict):
    """清单的主体。"""

    skills: SkillsSpec
    model: ModelSpec = ModelSpec()
    policy: PolicySpec = PolicySpec()
    connectors: tuple[ConnectorSpec, ...] = ()
    evals: tuple[EvalCase, ...] = ()

    @model_validator(mode="after")
    def _cross_references(self) -> PackageSpec:
        names = [connector.name for connector in self.connectors]
        if len(set(names)) != len(names):
            raise ValueError("connectors 里有重名的连接器")
        eval_names = [case.name for case in self.evals]
        if len(set(eval_names)) != len(eval_names):
            raise ValueError("evals 里有重名的用例")
        for case in self.evals:
            if case.entry not in self.skills.entries:
                raise ValueError(f"评测用例 {case.name!r} 的入口 {case.entry!r} 不在 entries 里")
        return self

    @property
    def secret_names(self) -> tuple[str, ...]:
        """整个包引用的全部密钥名。"""
        found = {name for connector in self.connectors for name in connector.secret_names}
        return tuple(sorted(found))


class PackageManifest(_Strict):
    """业务包清单。"""

    api_version: Annotated[Literal["xuanpu/v1"], Field(alias="apiVersion")]
    kind: Literal["Package"] = "Package"
    metadata: Metadata
    spec: PackageSpec

    @property
    def name(self) -> str:
        """包名。"""
        return self.metadata.name

    @property
    def version(self) -> str:
        """包版本。"""
        return self.metadata.version


def version_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    """语义化版本的排序键：先比主次修订号；正式版排在同号的预发布版之后。"""
    matched = re.match(_VERSION_PATTERN, version)
    if matched is None:
        raise ValueError(f"不是合法的语义化版本：{version!r}")
    major, minor, patch = (int(matched.group(index)) for index in (1, 2, 3))
    prerelease = matched.group(4)
    if prerelease is None:
        return major, minor, patch, 1, ()
    parts: list[tuple[int, int | str]] = []
    for part in prerelease[1:].split("."):
        # 纯数字段按数值比较，且排在含字母的段之前
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return major, minor, patch, 0, tuple(parts)


__all__ = [
    "API_VERSION",
    "MANIFEST_FILENAME",
    "ApprovalLevel",
    "BudgetSpec",
    "ConnectorSpec",
    "ConnectorTool",
    "Effect",
    "EnvValue",
    "EvalCase",
    "EvalExpectation",
    "HttpAuth",
    "HttpAuthType",
    "Metadata",
    "ModelSpec",
    "ModelTier",
    "PackageManifest",
    "PackageSpec",
    "PolicySpec",
    "SandboxSpec",
    "SkillsSpec",
    "Transport",
    "version_key",
]
