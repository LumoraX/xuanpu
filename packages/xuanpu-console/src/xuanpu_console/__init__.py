"""xuanpu-console —— 控制台的静态页面。"""

from __future__ import annotations

from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

__all__ = ["__version__", "static_dir"]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("xuanpu-console")


def static_dir() -> Path:
    """静态页面所在的目录，供服务挂载。"""
    return Path(str(files("xuanpu_console").joinpath("static")))
