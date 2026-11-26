"""Google 通讯录 OAuth 关联配置（默认关闭，不配置凭据不发任何外部请求）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from server.domains.identity.config import normalize_origin

DEFAULT_GOOGLE_SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/contacts.readonly",
)


class GoogleContactsConfig(BaseModel):
    """个人空间 Google 通讯录只读关联配置。

    默认 ``enabled=False``；只有显式开启并配置了 ``client_id``、``client_secret`` 与平台
    ``public_url`` 后，用户才能发起 OAuth 授权跳转与同步。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    auth_base: str = "https://accounts.google.com"
    token_base: str = "https://oauth2.googleapis.com"  # noqa: S105
    people_api_base: str = "https://people.googleapis.com"
    scopes: tuple[str, ...] = DEFAULT_GOOGLE_SCOPES

    @field_validator("auth_base", "token_base", "people_api_base")
    @classmethod
    def _validate_origin(cls, value: str) -> str:
        """与飞书配置一致：只接受 scheme://host[:port] 形式的源地址。"""
        return normalize_origin(value, name="google_contacts")

    def is_configured(self, public_url: str | None) -> bool:
        """判断是否已具备完整 OAuth 配置（启用 + 凭据 + 平台回调域名）。"""
        return bool(
            self.enabled
            and self.client_id.strip()
            and self.client_secret.strip()
            and public_url
        )
