"""微内核插件体系数据模型：后端插件清单、运行态快照、零 DDL 集合条目与前端插件描述符（ADR 0027）。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 —— Pydantic 运行时解析字段类型
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PluginTrustLevel = Literal["in_process_verified", "isolated_worker"]
PluginState = Literal["registered", "mounted", "draining", "unmounted", "failed"]
UiSlotKind = Literal["header-pill", "message-renderer", "aux-tab", "artifact-viewer"]
UiPluginMode = Literal["declarative", "sandbox_iframe"]


class BackendPluginManifest(BaseModel):
    """后端插件静态声明清单（强类型、禁止未知字段）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    plugin_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    version: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    trust_level: PluginTrustLevel = "in_process_verified"
    permissions: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    min_kernel_version: str = "0.1.0"


class PluginHealth(BaseModel):
    """插件健康探活结果，供 L0/L3 探针与回退前预检调用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    detail: str = Field(default="ok", max_length=300)
    checked_at: datetime


class PluginRecord(BaseModel):
    """插件运行态对外视图快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    plugin_id: str
    version: str
    display_name: str
    description: str
    trust_level: PluginTrustLevel
    state: PluginState
    permissions: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    last_error: str | None = None
    mounted_at: datetime | None = None


class PluginCollectionItem(BaseModel):
    """零 DDL 插件扩展集合中的单条记录（按 tenant + user + plugin + collection 强隔离）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    record_id: str = Field(min_length=1, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=80)
    plugin_id: str = Field(min_length=1, max_length=128)
    collection: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    created_at: float
    updated_at: float


class UiPluginDescriptor(BaseModel):
    """下发给 Web 主壳的前端插件描述符（涵盖 L2 内置插件与 L1 匠人包自带 ui/ 插件）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    plugin_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=80)
    slot: UiSlotKind
    mode: UiPluginMode
    source_layer: Literal["L1_artisan", "L2_builtin"]
    package: str | None = None
    version: str = Field(min_length=1, max_length=64)
    entry_url: str | None = None
    mime_types: tuple[str, ...] = ()
    bridge_permissions: tuple[str, ...] = ()
