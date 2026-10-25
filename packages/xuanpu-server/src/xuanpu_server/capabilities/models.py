"""登记、预检和安装状态；当前阶段不存在有效发布或运行授权。"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from .delivery import Delivery, StrictModel


class Record(StrictModel):
    """全域记录必须携带租户、固定ID及版本。"""
    tenant_id: str
    id: str
    version: int = Field(default=1, ge=1)
    created_at: float


class CapabilityRevision(Record):
    """同一能力的每份修订只追加，描述不可覆盖。"""
    capability_id: str
    capability_key: str
    revision: int = Field(ge=1)
    digest: str
    delivery: Delivery


class InstallPreview(Record):
    """预检中的秘密变化标记只存内部，公开视图必须按白名单构造。"""
    capability_id: str
    revision: int
    expected_version: int
    expires_at: float
    public_config: dict[str, JsonValue]
    secret_refs: dict[str, str]
    connection_refs: dict[str, str]
    evidence: dict[str, JsonValue]
    evidence_digest: str
    requirements: tuple[str, ...]
    diff: dict[str, JsonValue]


class InstallationConfig(Record):
    """每次安装保存不可变配置快照，不修改此前配置。"""
    capability_id: str
    revision: int
    config_revision: int
    public_config: dict[str, JsonValue]
    secret_refs: dict[str, str]
    connection_refs: dict[str, str]
    evidence: dict[str, JsonValue]
    digest: str


class Installation(Record):
    """ready由R4-C发布事务确立，A阶段只允许staged或停止状态。"""
    capability_id: str
    config_id: str
    config_revision: int
    status: Literal['staged', 'ready', 'draining', 'disabled'] = 'staged'


class CapabilityState(StrictModel):
    """同一协调事务内的登记、预检、配置和安装。"""
    revisions: dict[str, CapabilityRevision] = Field(default_factory=dict)
    previews: dict[str, InstallPreview] = Field(default_factory=dict)
    configurations: dict[str, InstallationConfig] = Field(default_factory=dict)
    installations: dict[str, Installation] = Field(default_factory=dict)
