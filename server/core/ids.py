"""标识生成。"""

from __future__ import annotations

import secrets


def new_id(prefix: str) -> str:
    """生成带前缀的随机标识，如 ``ses_3f9c…``。96 位随机数，碰撞概率可以忽略。"""
    return f"{prefix}_{secrets.token_hex(12)}"


__all__ = ["new_id"]
