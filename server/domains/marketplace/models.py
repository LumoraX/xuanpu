"""能力市场数据模型：匠人与通用技能库的目录项与安装结果。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

MarketplaceKind = Literal["artisan", "skill_library"]


@dataclass(frozen=True)
class CatalogSkill:
    """能力包内单个 skill 的摘要信息（用于渐进展示与分类浏览）。"""

    skill_id: str
    display_name: str
    category: str | None
    tags: tuple[str, ...]
    description: str
    exported: bool
    entry: bool

    def to_dict(self) -> dict[str, object]:
        """序列化为接口字典。"""
        return {
            "skill_id": self.skill_id,
            "display_name": self.display_name,
            "category": self.category,
            "tags": list(self.tags),
            "description": self.description,
            "exported": self.exported,
            "entry": self.entry,
        }


@dataclass(frozen=True)
class CatalogItem:
    """市场目录中的一条能力记录（匠人包或通用技能库）。"""

    name: str
    version: str
    kind: MarketplaceKind
    display_name: str
    description: str
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    skills: tuple[CatalogSkill, ...]
    uses: tuple[str, ...]
    required_secrets: tuple[str, ...]
    connectors: tuple[str, ...]
    source_path: Path | None
    is_archive: bool = False
    source: Literal["catalog", "tenant"] = "catalog"


__all__ = ["CatalogItem", "CatalogSkill", "MarketplaceKind"]
