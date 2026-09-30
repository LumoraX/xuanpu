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


class ConnectorSpec(_Strict):
    """一个 MCP 连接器。

    Attributes:
        name: 连接器名。模型看到的工具名是 ``mcp__<连接器名>__<工具名>``。
        transport: 传输方式。
        command: ``stdio`` 传输下启动 MCP server 的命令。
        env: 传给 MCP server 进程的环境变量。
        effect: 该连接器工具的默认副作用分类；默认按最保守的 ``external``。
        tools: 工具白名单与逐工具的副作用分类。留空表示开放该 server 的全部工具。
    """

    name: Name
    transport: Literal["stdio"] = "stdio"
    command: Annotated[tuple[str, ...], Field(min_length=1)]
    env: dict[EnvName, EnvValue] = Field(default_factory=dict)
    effect: Effect = "external"
    tools: tuple[ConnectorTool, ...] = ()

    @model_validator(mode="after")
    def _tools_unique(self) -> ConnectorSpec:
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tools 里有重复的工具名")
        return self

    def effect_of(self, tool_name: str) -> Effect:
        """某个工具的副作用分类：逐工具声明优先，否则用连接器默认值。"""
        for tool in self.tools:
            if tool.name == tool_name and tool.effect is not None:
                return tool.effect
        return self.effect

    def allows(self, tool_name: str) -> bool:
        """该工具是否在白名单内（未声明白名单则全部放行）。"""
        return not self.tools or any(tool.name == tool_name for tool in self.tools)

    @property
    def secret_names(self) -> tuple[str, ...]:
        """该连接器引用的全部密钥名。"""
        return tuple(sorted({item.secret for item in self.env.values() if item.secret}))


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
    "Metadata",
    "ModelSpec",
    "ModelTier",
    "PackageManifest",
    "PackageSpec",
    "PolicySpec",
    "SandboxSpec",
    "SkillsSpec",
    "version_key",
]
