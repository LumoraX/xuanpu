"""玄圃自有账号的记录类型（ADR 0019）：用户名口令凭据、一次性设置链接，以及用户名的规整。

与身份域其余记录一样不可变，取值受限的字段在构造时校验，从库里读回的脏数据当场报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, get_args

from server.domains.identity.models import require_choice

AccountTokenPurpose = Literal["setup", "reset"]
"""设置链接的用途：``setup`` 是开通后首次设口令，``reset`` 是管理员发起的重置。"""

USERNAME_MIN_LENGTH = 2
USERNAME_MAX_LENGTH = 64
# 只收 ASCII 字母数字与 . _ -，首字符必须是字母或数字；规整后一律小写
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def normalize_username(value: str) -> str:
    """把管理员给的用户名规整成存储与比较用的形式：去首尾空白、转小写，再校验字符集与长度。

    用户名只用 ASCII，避免同形字符冒充（如全角字母、西里尔字母）；不区分大小写。

    Raises:
        ValueError: 长度不在 2–64，或含字母、数字、``.``、``_``、``-`` 之外的字符，或首字符不是
            字母或数字。
    """
    name = value.strip().lower()
    if not USERNAME_MIN_LENGTH <= len(name) <= USERNAME_MAX_LENGTH:
        raise ValueError(
            f"用户名长度必须在 {USERNAME_MIN_LENGTH}–{USERNAME_MAX_LENGTH} 个字符之间"
        )
    if not _USERNAME.match(name):
        raise ValueError("用户名只能含英文字母、数字与 . _ -，并以字母或数字开头")
    return name


@dataclass(frozen=True)
class LocalCredential:
    """一名员工的用户名口令凭据。``(tenant_id, user_id)`` 与 ``(tenant_id, username)`` 各自唯一。

    Attributes:
        tenant_id: 所属租户。
        user_id: 对应的平台用户。
        username: 规整后的用户名（``normalize_username``）。
        password_hash: 口令的 argon2id 摘要（PHC 字符串）；为空表示已开通、尚未设口令。
        created_at: 开通的时间。
        password_changed_at: 最近一次设口令的时间；未设过为空。
        failed_attempts: 连续登录失败次数；登录成功或设口令时清零。
        locked_until: 锁定到的时刻；未锁定为空。
    """

    tenant_id: str
    user_id: str
    username: str
    created_at: float
    password_hash: str | None = field(default=None, repr=False)
    password_changed_at: float | None = None
    failed_attempts: int = 0
    locked_until: float | None = None

    def __post_init__(self) -> None:
        """用户名必须已经规整；设了口令就必须有设口令的时间；失败次数不能为负。"""
        if normalize_username(self.username) != self.username:
            raise ValueError(f"用户名必须是规整后的形式，得到 {self.username!r}")
        if self.password_hash is not None and self.password_changed_at is None:
            raise ValueError("设了口令的凭据必须带 password_changed_at")
        if self.failed_attempts < 0:
            raise ValueError(f"failed_attempts 不能为负，得到 {self.failed_attempts}")

    @property
    def has_password(self) -> bool:
        """是否已经设了口令。"""
        return self.password_hash is not None

    def locked(self, now: float) -> bool:
        """在 ``now`` 这一刻是否处于锁定期。"""
        return self.locked_until is not None and self.locked_until > now


@dataclass(frozen=True)
class AccountToken:
    """一条一次性设置链接。只存令牌的 SHA-256；每名员工同时只有一条。

    Attributes:
        token_hash: 令牌的摘要；令牌本身只出现在交给管理员的链接里。
        tenant_id: 所属租户。
        user_id: 链接设的是谁的口令。
        purpose: ``setup`` 或 ``reset``。
        created_at: 签发时间。
        expires_at: 到期时刻；有效期由服务层算好。
        issued_by: 签发者（审计里的 ``actor``）。
    """

    token_hash: str = field(repr=False)
    tenant_id: str
    user_id: str
    purpose: AccountTokenPurpose
    created_at: float
    expires_at: float
    issued_by: str

    def __post_init__(self) -> None:
        """用途取值受限；到期时刻必须晚于签发时间。"""
        require_choice("purpose", self.purpose, get_args(AccountTokenPurpose))
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at 必须晚于 created_at")


__all__ = [
    "USERNAME_MAX_LENGTH",
    "USERNAME_MIN_LENGTH",
    "AccountToken",
    "AccountTokenPurpose",
    "LocalCredential",
    "normalize_username",
]
