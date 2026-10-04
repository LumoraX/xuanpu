"""xuanpu-sdk-py —— xuanpu 平台的 Python 客户端。"""

from __future__ import annotations

from importlib.metadata import version

from xuanpu_sdk.client import XuanpuClient
from xuanpu_sdk.errors import XuanpuError, XuanpuTransportError
from xuanpu_sdk.models import (
    AuditRecord,
    Event,
    IssuedKey,
    PackageVersion,
    Session,
    Usage,
    UserUsage,
)

__all__ = [
    "AuditRecord",
    "Event",
    "IssuedKey",
    "PackageVersion",
    "Session",
    "Usage",
    "UserUsage",
    "XuanpuClient",
    "XuanpuError",
    "XuanpuTransportError",
    "__version__",
]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("xuanpu-sdk-py")
