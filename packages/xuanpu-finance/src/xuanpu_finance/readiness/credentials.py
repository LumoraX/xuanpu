"""离线预检和正式启动共用的固定文件读取与不可变凭据快照。"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.config import FinanceConfig

MAX_SECRET_BYTES = 65536


def read_fixed(path: Path, *, private: bool = True) -> tuple[bytes, tuple[int, int]]:
    """禁止符号链接和特殊文件；单次有界读取并检查私密权限。"""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (private and stat.S_IMODE(info.st_mode) & ~0o600):
            raise ValueError("secret_file_invalid")
        data = os.read(descriptor, MAX_SECRET_BYTES + 1)
        if not data or len(data) > MAX_SECRET_BYTES:
            raise ValueError("secret_file_invalid")
        return data, (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)


def read_text_secret(path: Path) -> tuple[bytes, tuple[int, int]]:
    """有界读取文本凭据，按运行时规则严格解码并拒绝空白内容。"""
    raw, identity = read_fixed(path)
    normalized = raw.decode("utf-8").strip().encode("utf-8")
    if not normalized:
        raise ValueError("text_secret_invalid")
    return normalized, identity


def valid_dsn(raw: bytes) -> bool:
    """只接受显式PostgreSQL地址，不调用驱动或解析环境变量。"""
    parts = urlsplit(raw.decode().strip())
    return bool(
        parts.scheme in {"postgres", "postgresql"}
        and parts.hostname
        and parts.username
        and parts.path not in {"", "/"}
        and not parts.fragment
        and not parts.query
        and (parts.port is None or 1 <= parts.port <= 65535)
    )


def tls_origin(value: str) -> bool:
    """正式对象及网关端点须为无嵌入凭据的有效TLS地址。"""
    try:
        parts = urlsplit(value)
        return bool(
            parts.scheme == "https"
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and (parts.port is None or 1 <= parts.port <= 65535)
        )
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class CredentialSnapshot:
    """只保存已验不可变值，所有秘密字段均排除repr。"""

    dsn: str = field(repr=False)
    service_token: str = field(repr=False)
    gateway_tokens: tuple[tuple[str, str], ...] = field(repr=False)
    public_keys: tuple[tuple[str, str], ...] = field(repr=False)
    aes_key: bytes = field(repr=False)
    access_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)


def capture_credentials(config: FinanceConfig) -> CredentialSnapshot:
    """一次读取所有固定凭据并检查格式、身份和令牌分离。"""
    dsn, _ = read_text_secret(config.database_dsn_file)
    aes, _ = read_fixed(config.objects.key_file)
    if not valid_dsn(dsn) or len(aes) != 32:
        raise ValueError("credential_invalid")
    keys = []
    for kid, path in config.public_key_files.items():
        public, _ = read_fixed(path, private=False)
        if not isinstance(serialization.load_pem_public_key(public), Ed25519PublicKey):
            raise ValueError("credential_invalid")
        keys.append((kid, public.decode("utf-8").strip()))
    service, gateways = capture_tokens(config)
    access = secret = None
    if config.objects.kind == "s3" and (
        config.objects.access_key_file is None or config.objects.secret_key_file is None
    ):
        raise ValueError("credential_invalid")
    if config.objects.access_key_file is not None:
        access = read_text_secret(config.objects.access_key_file)[0].decode("utf-8")
    if config.objects.secret_key_file is not None:
        secret = read_text_secret(config.objects.secret_key_file)[0].decode("utf-8")
    return CredentialSnapshot(dsn.decode("utf-8"), service, gateways, tuple(keys), aes, access, secret)


def capture_tokens(config: FinanceConfig) -> tuple[str, tuple[tuple[str, str], ...]]:
    """服务、租户网关与供应商令牌按inode和规范化字节独立。"""
    values: set[bytes] = set()
    identities: set[tuple[int, int]] = set()

    def token(path: Path) -> str:
        """每个令牌固定文件只读取一次，随后记录不可变规范化值。"""
        normalized, identity = read_text_secret(path)
        if normalized in values or identity in identities:
            raise ValueError("token_separation_invalid")
        values.add(normalized)
        identities.add(identity)
        return normalized.decode("utf-8")

    service = token(config.service_token_file)
    gateways = tuple((tenant, token(path)) for tenant, path in config.gateway.tenant_token_files.items())
    for provider in [*config.settlement_providers, *config.compliance_providers]:
        token(provider.token_file)
        callback, _ = read_text_secret(provider.callback_secret_file)
        if len(callback) < 32:
            raise ValueError("callback_secret_invalid")
    return service, gateways
