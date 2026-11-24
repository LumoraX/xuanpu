"""业务包给员工看的多语言显示名。"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field, StringConstraints

LocaleCode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*$")]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=40, pattern=r"^[^\r\n]+$")]
LocalizedDisplayName = Annotated[dict[LocaleCode, DisplayName], Field(min_length=1, max_length=20)]
DisplayNameValue = DisplayName | LocalizedDisplayName
_LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*$")


def valid_locale(value: object) -> bool:
    """判断语言标签是否为本规范支持的 BCP 47 常用形式。"""
    return isinstance(value, str) and _LOCALE_PATTERN.fullmatch(value) is not None


def display_name_for(value: DisplayNameValue | None, locale: str | None) -> str | None:
    """按当前语言选择显示名；精确语言、语言基名、英文和首个声明依次回退。"""
    if value is None or isinstance(value, str):
        return value
    normalized = (locale or "en-US").replace("_", "-").lower()
    choices = (normalized, normalized.split("-", 1)[0], "en-us", "en")
    lowered = {key.lower(): name for key, name in value.items()}
    for choice in choices:
        if choice in lowered:
            return lowered[choice]
    return next(iter(value.values()))


__all__ = [
    "DisplayName",
    "DisplayNameValue",
    "LocaleCode",
    "LocalizedDisplayName",
    "display_name_for",
    "valid_locale",
]
