"""xuanpu-cli —— xuanpu 命令行。"""

from __future__ import annotations

from importlib.metadata import version

__all__ = ["__version__"]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("xuanpu-cli")
