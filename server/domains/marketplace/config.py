"""能力市场配置：匠人市场与通用技能库目录来源。"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 —— pydantic 在运行时解析字段类型

from pydantic import BaseModel, ConfigDict


class MarketplaceConfig(BaseModel):
    """能力市场配置段。

    Attributes:
        catalog_dirs: 本地能力目录或归档目录列表。每个目录可包含带 ``xuanpu.yaml`` 的包目录
            或打包好的 ``.zip`` 归档，按清单中的 ``is_library`` 自动归入「技能市场」或「匠人市场」。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_dirs: tuple[Path, ...] = ()


__all__ = ["MarketplaceConfig"]
