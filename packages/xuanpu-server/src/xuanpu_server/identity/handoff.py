"""桌面接力记录的构造与确认码（ADR 0019）；流程见 ``identity.desktop``。

单独成模块：``identity.login`` 在飞书回调时建接力记录，``identity.desktop`` 又依赖 ``identity.login``，
放在这里两边都能直接导入，不成环。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from xuanpu_server.identity.cookies import login_token_hash
from xuanpu_server.identity.models import DesktopHandoff

if TYPE_CHECKING:
    from xuanpu_server.identity.models import LoginAttempt

HANDOFF_TTL_SECONDS = 300
"""接力记录的有效期：飞书回调后 5 分钟内要完成确认与换取。"""


def confirm_code(cookie_hash: str) -> str:
    """由登录尝试 cookie 的摘要算出 6 位确认码：客户端等待页与浏览器确认页各自算出同一个值。

    确认码只供员工肉眼核对，不是凭据：换取靠 cookie，确认靠确认串。
    """
    return f"{int(cookie_hash[:12], 16) % 1_000_000:06d}"


def new_handoff(attempt: LoginAttempt, user_id: str, *, now: float) -> tuple[DesktopHandoff, str]:
    """为一次桌面登录建接力记录，返回 ``(记录, 确认串)``；库里只存确认串的摘要。"""
    nonce = secrets.token_urlsafe(32)
    handoff = DesktopHandoff(
        cookie_hash=attempt.cookie_hash,
        tenant_id=attempt.tenant_id,
        user_id=user_id,
        return_to=attempt.return_to,
        nonce_hash=login_token_hash(nonce),
        confirmed=False,
        created_at=now,
        expires_at=now + HANDOFF_TTL_SECONDS,
    )
    return handoff, nonce


__all__ = ["HANDOFF_TTL_SECONDS", "confirm_code", "new_handoff"]
