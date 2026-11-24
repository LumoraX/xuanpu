"""独立于包归档的严格交付描述，不包含人员或秘密值。"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

Name = Annotated[str, StringConstraints(strict=True, pattern=r'^[a-z][a-z0-9-]{0,62}$')]
Identifier = Annotated[str, StringConstraints(strict=True, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r'^[a-f0-9]{64}$')]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]


class StrictModel(BaseModel):
    """未知字段不得静默进入模板，持久记录使用不可变模型。"""
    model_config = ConfigDict(frozen=True, extra='forbid')


class PackageReference(StrictModel):
    """固定包和入口，不能引用活动指针作为版本。"""
    name: Name
    version: Annotated[str, StringConstraints(pattern=r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$', max_length=100)]
    digest: Annotated[str, StringConstraints(pattern=r'^sha256:[a-f0-9]{64}$')]
    entry: Identifier


class InputContract(StrictModel):
    """执行格式由服务端契约决定，不能载入自定义验证代码。"""
    contract: Literal['office_files', 'capability_receipt', 'read_only_answer']
    mime_types: tuple[Annotated[str, StringConstraints(max_length=120)], ...] = Field(default=(), max_length=8)
    max_files: StrictInt = Field(default=8, ge=0, le=8)


class OutputContract(StrictModel):
    """R4-B才将固定契约接到任务完成核验。"""
    contract: Literal['office_files', 'capability_receipt', 'read_only_answer']
    output_kind: Literal['meeting_minutes', 'table_summary', 'capability_result', 'answer']

    @model_validator(mode='after')
    def matching_kind(self) -> OutputContract:
        """禁止文件成果借用只读回执或任意完成表达式。"""
        allowed = {'office_files': {'meeting_minutes', 'table_summary'},
                   'capability_receipt': {'capability_result'}, 'read_only_answer': {'answer'}}
        if self.output_kind not in allowed[self.contract]:
            raise ValueError('输出类型与完成契约不一致')
        return self


class ToolDeclaration(StrictModel):
    """工具集合只声明固定名称、参数结构摘要及副作用。"""
    name: Identifier
    schema_sha256: Digest
    effect: Literal['read', 'write', 'external']


class ConnectionRequirement(StrictModel):
    """外部安装只使用可替换的只读连接契约。"""
    connector: Name
    kind: Literal['wecom']
    operations: tuple[Literal['self_profile.read', 'directory_preview.read'], ...] = Field(min_length=1, max_length=2)


class Permissions(StrictModel):
    """已发布包需求必须与声明相符，预检不扩大许可。"""
    shell: StrictBool
    network: StrictBool
    approval: Literal['strict', 'standard', 'relaxed']
    model_tier: Literal['fast', 'balanced', 'deep']
    max_tokens: StrictInt = Field(ge=1000, le=100000000)
    max_iterations: StrictInt = Field(ge=1, le=500)
    tools: tuple[ToolDeclaration, ...] = Field(default=(), max_length=100)
    connections: tuple[ConnectionRequirement, ...] = Field(default=(), max_length=20)

    @model_validator(mode='after')
    def unique_names(self) -> Permissions:
        """重复声明不能用先后顺序覆盖权限。"""
        if len({t.name for t in self.tools}) != len(self.tools):
            raise ValueError('工具声明重复')
        if len({c.connector for c in self.connections}) != len(self.connections):
            raise ValueError('连接声明重复')
        return self


class ConfigRequirement(StrictModel):
    """本地公开参数有明确类型及枚举，不接受执行内容。"""
    kind: Literal['string', 'integer', 'boolean']
    required: StrictBool = True
    choices: tuple[Text, ...] = Field(default=(), max_length=100)


class Requirements(StrictModel):
    """模板仅声明参数槽和秘密引用槽，不携带值。"""
    public_config: dict[Identifier, ConfigRequirement] = Field(default_factory=dict, max_length=30)
    secret_refs: tuple[Name, ...] = Field(default=(), max_length=30)

    @model_validator(mode='after')
    def safe_slots(self) -> Requirements:
        """身份和凭据不能伪装成公开配置参数。"""
        forbidden = {'tenant_id', 'actor', 'user_id', 'password', 'secret', 'token', 'api_key'}
        if forbidden & self.public_config.keys() or len(set(self.secret_refs)) != len(self.secret_refs):
            raise ValueError('配置槽非法或重复')
        return self


class Delivery(StrictModel):
    """交付描述不进入其自身引用的归档，避免摘要自引用。"""
    format: Literal['xuanpu-delivery/v1']
    capability_key: Name
    title: Text
    summary: Text
    scenario_key: Name
    package: PackageReference
    inputs: InputContract
    outputs: OutputContract
    permissions: Permissions
    requirements: Requirements
    evaluation_suite_sha256: Digest


def fingerprint(value: object) -> str:
    """规范JSON摘要只用于公开配置及内部密文版本，不计算秘密明文摘要。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()
