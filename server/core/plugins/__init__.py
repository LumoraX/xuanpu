"""玄圃微内核后端插件总线（ADR 0027）。

对外导出统一契约 ``BackendPlugin``、宿主 ``PluginHost``、受限上下文 ``PluginContext``、
零 DDL 扩展集合存储 ``PluginCollectionStore`` 及核心数据模型。
"""

from __future__ import annotations

from server.core.plugins.collections import PluginCollectionStore
from server.core.plugins.host import (
    BackendPlugin,
    PluginContext,
    PluginEventHandler,
    PluginHost,
    PluginHttpHandler,
    PluginToolDef,
)
from server.core.plugins.models import (
    BackendPluginManifest,
    PluginCollectionItem,
    PluginHealth,
    PluginRecord,
    PluginState,
    PluginTrustLevel,
    UiPluginDescriptor,
)

__all__ = [
    "BackendPlugin",
    "BackendPluginManifest",
    "PluginCollectionItem",
    "PluginCollectionStore",
    "PluginContext",
    "PluginEventHandler",
    "PluginHealth",
    "PluginHost",
    "PluginHttpHandler",
    "PluginRecord",
    "PluginState",
    "PluginToolDef",
    "PluginTrustLevel",
    "UiPluginDescriptor",
]
