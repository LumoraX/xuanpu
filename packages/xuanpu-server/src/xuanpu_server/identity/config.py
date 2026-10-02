"""员工身份的平台级配置：``identity`` 配置段，以及对外地址（源）的规整。

服务配置（``xuanpu_server.config``）引用这里：``ServerConfig.identity`` 是 ``IdentityConfig``，
``ServerConfig.public_url`` 与飞书的两个基地址都经 ``normalize_origin`` 规整。依赖方向是
``config`` → ``identity.config``，本模块不反过来引用服务配置。

默认值只在 ``IdentityConfig`` 里写一份；运行时用的 ``IdentitySettings`` 由它构造
（``IdentitySettings.from_config``）。
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.tenancy import validate_tenant_id

if TYPE_CHECKING:
    from pydantic import ValidationInfo

# 站内路径：以 / 开头，第二个字符不是 / 或 \（否则浏览器会当成别的站点），全程不含 \ 与空白。
# pydantic 的正则引擎不支持前瞻，所以写成「只有 /，或 / 后跟一个合格的首字符」
SITE_PATH = r"^/([^/\\\s][^\\\s]*)?$"
_HOST_NAME = re.compile(r"^[a-z0-9.-]+$")


def _normalized_host(host: str, *, name: str, value: str) -> str:
    """主机名（已转小写）：只含小写字母、数字、点与连字符，或是 IP 字面量；IPv6 加回方括号。"""
    if ":" in host:
        # 只有方括号里的 IPv6 字面量会带冒号；urlsplit 已经核对过它是合法地址，这里再确认一次
        return f"[{ipaddress.IPv6Address(host).compressed}]"
    if not _HOST_NAME.match(host):
        raise ValueError(
            f"{name} 的主机名只能含字母、数字、点与连字符（国际化域名写成 punycode），"
            f"或是 IP 地址，得到 {value!r}"
        )
    return host


def normalize_origin(value: str, *, name: str) -> str:
    """把只写了源的地址规整成浏览器 ``Origin`` 头的写法：``scheme://host[:port]``。

    协议与主机名转小写，去掉默认端口（http 的 80、https 的 443）与末尾的 ``/``。拒绝：
    非 ``http``/``https``；带用户名密码、路径、查询串或片段；任何空白与控制字符（``urlsplit``
    会悄悄删掉它们）；``%`` 编码；主机名含字母、数字、点、连字符之外的字符（IPv6 要写在方括号
    里）；端口不在 1–65535。规整是幂等的。

    Raises:
        ValueError: 不合要求；``name`` 用在提示里。
    """
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{name} 不能含空白或控制字符，得到 {value!r}")
    if "%" in value:
        raise ValueError(f"{name} 不能含 % 编码，得到 {value!r}")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{name} 必须是 http:// 或 https:// 开头的地址，得到 {value!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} 不能带用户名或密码")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(
            f"{name} 只写协议、主机与端口（如 https://xuanpu.example.com），不带路径、查询串或"
            f"片段，得到 {value!r}"
        )
    host = _normalized_host(parts.hostname, name=name, value=value)
    port = parts.port  # 不是数字或大于 65535 时这里报 ValueError
    if port == 0:
        raise ValueError(f"{name} 的端口必须在 1–65535 之间，得到 {value!r}")
    default_port = 80 if parts.scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parts.scheme}://{host}{suffix}"


class IdentityConfig(BaseModel):
    """员工身份（飞书登录）的平台级设置。租户自己的飞书应用经接口配置，不在这里。

    Attributes:
        session_ttl_seconds: 登录会话的默认有效期（秒）；租户的飞书配置可以覆盖。
        cookie_secure: 登录 cookie 是否只走 HTTPS。为真时 cookie 名带 ``__Host-`` 前缀
            （``__Host-xuanpu_session``）；本地用 HTTP 开发时设为 false，cookie 名是
            ``xuanpu_session``（见 ``identity.cookies``）。
        reconcile_interval_seconds: 按飞书通讯录对账的周期（秒）。
        default_tenant: 登录链接省略 ``tenant`` 时用的租户。
        login_landing_path: 登录完成或失败后的默认落地页，站内路径：以 ``/`` 开头、不以
            ``//`` 或 ``/\\`` 开头，不含反斜杠与空白（否则可能被浏览器当成别的站点）。
        feishu_open_base: 飞书开放接口的基地址；测试时指向模拟服务端，也可用于接 Lark 国际版。
            与 ``public_url`` 一样只写源。
        feishu_accounts_base: 飞书授权页的基地址，同上。
    """

    # 与服务配置的各段相同：拼错的键要报错，取值不可变
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ttl_seconds: int = Field(default=604800, ge=60)
    cookie_secure: bool = True
    reconcile_interval_seconds: float = Field(default=86400.0, ge=60)
    default_tenant: str | None = None
    login_landing_path: str = Field(default="/console/", pattern=SITE_PATH)
    feishu_open_base: str = "https://open.feishu.cn"
    feishu_accounts_base: str = "https://accounts.feishu.cn"

    @field_validator("default_tenant")
    @classmethod
    def _tenant_id_shape(cls, value: str | None) -> str | None:
        """租户标识的字符集与平台其余各处一致。"""
        return validate_tenant_id(value) if value is not None else None

    @field_validator("feishu_open_base", "feishu_accounts_base")
    @classmethod
    def _origin_only(cls, value: str, info: ValidationInfo) -> str:
        """飞书的两个基地址只写源，规整后保存（拼接口路径时不会多出或少了 ``/``）。"""
        return normalize_origin(value, name=str(info.field_name))


__all__ = ["SITE_PATH", "IdentityConfig", "normalize_origin"]
