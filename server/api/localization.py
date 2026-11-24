"""HTTP 请求中的语言选择。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


def request_locale(request: Request) -> str:
    """取 Accept-Language 的首选语言；缺失时使用英文。"""
    header = request.headers.get("accept-language", "")
    value = header.split(",", 1)[0].split(";", 1)[0].strip()
    return value or "en-US"


__all__ = ["request_locale"]
