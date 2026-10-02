"""员工身份的配置：``public_url`` 与 ``identity`` 段的解析、校验与规整，以及运行时设置的构造。"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from xuanpu_server.config import ConfigError, parse_config
from xuanpu_server.identity.config import IdentityConfig, normalize_origin
from xuanpu_server.identity.service import IdentitySettings

_BASE: dict[str, Any] = {
    "data_dir": "/var/lib/xuanpu",
    "root_key_hash": "a" * 64,
    "master_keys": {"k1": base64.b64encode(b"k" * 32).decode()},
    "primary_master_key": "k1",
    "models": {
        "base_url": "http://gateway:4000/v1",
        "api_key": "sk-test",
        "tiers": {tier: {"model": "m"} for tier in ("fast", "balanced", "deep")},
    },
}


def _parse(**top: Any) -> Any:
    """在合法的最小配置上加几个顶层键，解析出 ``ServerConfig``。"""
    return parse_config(json.dumps({**_BASE, **top}), variables={})


def test_defaults() -> None:
    """不配 public_url 与 identity 段：没有对外地址，身份设置取设计里的默认值。"""
    config = _parse()
    assert config.public_url is None
    identity = config.identity
    assert (identity.session_ttl_seconds, identity.cookie_secure) == (604800, True)
    assert identity.reconcile_interval_seconds == 86400
    assert (identity.default_tenant, identity.login_landing_path) == (None, "/console/")
    assert identity.feishu_open_base == "https://open.feishu.cn"
    assert identity.feishu_accounts_base == "https://accounts.feishu.cn"


def test_settings_are_built_from_config() -> None:
    """运行时设置只由配置构造（默认值只写在配置里）；public_url 再规整一次，幂等。"""
    config = _parse(public_url="https://Xuanpu.Example.com/", identity={"cookie_secure": False})
    settings = IdentitySettings.from_config(config.public_url, config.identity)
    assert settings.public_url == "https://xuanpu.example.com"
    assert settings.cookie_secure is False
    assert settings.session_ttl_seconds == 604800
    assert IdentitySettings.from_config(
        "HTTPS://X.EXAMPLE.COM:443", IdentityConfig()
    ).public_url == ("https://x.example.com")
    assert IdentitySettings.from_config(None, IdentityConfig()).public_url is None
    with pytest.raises(ValueError, match="public_url"):
        IdentitySettings.from_config("", IdentityConfig())


@pytest.mark.parametrize(
    ("written", "origin"),
    [
        ("https://xuanpu.example.com", "https://xuanpu.example.com"),
        ("https://xuanpu.example.com/", "https://xuanpu.example.com"),
        ("HTTPS://Xuanpu.Example.COM:443", "https://xuanpu.example.com"),
        ("http://127.0.0.1:80/", "http://127.0.0.1"),
        ("http://127.0.0.1:8420", "http://127.0.0.1:8420"),
        ("https://xuanpu.example.com:8443", "https://xuanpu.example.com:8443"),
        ("http://[::1]:8420", "http://[::1]:8420"),
        ("http://[0:0:0:0:0:0:0:1]", "http://[::1]"),
        ("https://xn--fiq228c.example.com", "https://xn--fiq228c.example.com"),
    ],
)
def test_origin_is_normalized(written: str, origin: str) -> None:
    """规整成浏览器 Origin 头的写法：小写、去掉默认端口与末尾的 /；飞书基地址同理。"""
    config = _parse(public_url=written, identity={"feishu_open_base": written})
    assert config.public_url == origin
    assert config.identity.feishu_open_base == origin
    assert normalize_origin(origin, name="x") == origin


@pytest.mark.parametrize(
    ("written", "fragment"),
    [
        ("xuanpu.example.com", "http://"),
        ("ftp://xuanpu.example.com", "http://"),
        ("https://xuanpu.example.com/xuanpu", "不带路径"),
        ("https://xuanpu.example.com/?a=1", "不带路径"),
        ("https://xuanpu.example.com/#x", "不带路径"),
        ("https://user:pw@xuanpu.example.com", "用户名或密码"),
        (" https://xuanpu.example.com ", "空白"),
        ("https://xuanpu.example.com\t", "空白"),
        ("https://xuanpu.exa\nmple.com", "空白"),
        ("https://ex%61mple.com", "% 编码"),
        ("https://[fe80::1%25en0]", "% 编码"),
        ("https://玄圃.example.com", "punycode"),
        ("https://ex_ample.com", "主机名"),
        ("https://ex!ample.com", "主机名"),
        ("https://[zz]", "IPv4 or IPv6"),
        ("https://xuanpu.example.com:0", "1–65535"),
        ("https://xuanpu.example.com:65536", "out of range"),
        ("https://xuanpu.example.com:+80", "integer"),
    ],
)
def test_bad_origins_are_rejected(written: str, fragment: str) -> None:
    """不是只写了源的 http(s) 地址，或混进空白、% 编码、不合规的主机名与端口：报错，指到字段。"""
    with pytest.raises(ConfigError) as caught:
        _parse(public_url=written)
    assert "public_url" in str(caught.value)
    assert fragment in str(caught.value)


@pytest.mark.parametrize(
    ("identity", "fragment"),
    [
        ({"session_ttl_seconds": 10}, "session_ttl_seconds"),
        ({"session_ttl_seconds": 30 * 86400 + 1}, "session_ttl_seconds"),
        ({"reconcile_interval_seconds": 0}, "reconcile_interval_seconds"),
        ({"default_tenant": "Acme Corp"}, "租户标识非法"),
        ({"login_landing_path": "console/"}, "login_landing_path"),
        ({"login_landing_path": "//evil.example.com/"}, "login_landing_path"),
        ({"login_landing_path": "/\\evil.example.com/"}, "login_landing_path"),
        ({"login_landing_path": "/con sole/"}, "login_landing_path"),
        ({"login_landing_path": "https://evil.example.com/"}, "login_landing_path"),
        ({"feishu_open_base": "https://open.feishu.cn/open-apis"}, "不带路径"),
        ({"feishu_accounts_base": "accounts.feishu.cn"}, "feishu_accounts_base"),
        ({"cookie_secured": False}, "cookie_secured"),
    ],
)
def test_bad_identity_section_is_rejected(identity: dict[str, Any], fragment: str) -> None:
    """identity 段取值不合法、键拼错：报错，指到具体字段。"""
    with pytest.raises(ConfigError) as caught:
        _parse(identity=identity)
    assert fragment in str(caught.value)


def test_site_paths_and_default_tenant() -> None:
    """落地页是站内路径（可带查询串，可以只是 /），默认租户按租户标识的规则校验。"""
    identity = _parse(
        identity={"login_landing_path": "/console/?from=login", "default_tenant": "acme"}
    ).identity
    assert identity.login_landing_path == "/console/?from=login"
    assert identity.default_tenant == "acme"
    assert _parse(identity={"login_landing_path": "/"}).identity.login_landing_path == "/"
