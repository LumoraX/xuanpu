"""只读取显式固定凭据文件的可取消离线检查，不创建任何运行资源。"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from xuanpu_finance.operations import request_digest
from xuanpu_finance.readiness.checks import build_report
from xuanpu_finance.readiness.models import ReadinessCheck, ReadinessReport, check

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


def credential_resources(config: FinanceConfig) -> list[ReadinessCheck]:
    """捕获资源格式错误但永不输出路径、DSN、正文或异常消息。"""
    try:
        dsn, _ = read_fixed(config.database_dsn_file)
        aes, _ = read_fixed(config.objects.key_file)
        if not valid_dsn(dsn) or len(aes) != 32:
            raise ValueError("credential_invalid")
        for path in config.public_key_files.values():
            public, _ = read_fixed(path, private=False)
            if not isinstance(serialization.load_pem_public_key(public), Ed25519PublicKey):
                raise ValueError("credential_invalid")
        validate_tokens(config)
        for optional_path in (config.objects.access_key_file, config.objects.secret_key_file):
            if optional_path is not None:
                read_text_secret(optional_path)
    except (OSError, ValueError, UnicodeError, UnsupportedAlgorithm):
        return [check("credential_resources", False, "credential_resources_invalid")]
    return [check("credential_resources", True, "credential_resources_valid")]


def validate_tokens(config: FinanceConfig) -> None:
    """服务、平台网关和各供应商令牌不得复用文件或令牌字节。"""
    tokens = [config.service_token_file, *config.gateway.tenant_token_files.values()]
    providers = [*config.settlement_providers, *config.compliance_providers]
    tokens += [p.token_file for p in providers]
    values: set[bytes] = set()
    identities: set[tuple[int, int]] = set()
    for path in tokens:
        normalized, identity = read_text_secret(path)
        if normalized in values or identity in identities:
            raise ValueError("token_separation_invalid")
        values.add(normalized)
        identities.add(identity)
    for provider in providers:
        callback, _ = read_text_secret(provider.callback_secret_file)
        if len(callback) < 32:
            raise ValueError("callback_secret_invalid")


def offline_snapshot(config: FinanceConfig, tenant: str, entity: str) -> dict[str, Any]:
    """只在内存准备同公司配置快照，秘密文件引用不进入摘要。"""
    policies = {}
    for kind in ("expense", "settlement", "compliance", "funding"):
        for policy in getattr(config, kind + "_policies"):
            if (policy.tenant_id, policy.legal_entity_id) == (tenant, entity):
                policies[kind] = policy.model_dump(mode="json")
    return {
        "scope": {"tenant_id": tenant, "legal_entity_id": entity},
        "company": [
            c.model_dump()
            for c in config.companies
            if (c.tenant_id, c.legal_entity_id) == (tenant, entity)
        ],
        "roles": [
            g.model_dump()
            for g in config.grants
            if (g.tenant_id, g.legal_entity_id) == (tenant, entity)
        ],
        "policies": policies,
        "simulation": config.execution_simulation,
        "providers": [
            p.public_descriptor()
            for p in [*config.settlement_providers, *config.compliance_providers]
            if (p.tenant_id, p.legal_entity_id) == (tenant, entity)
        ],
        "catalog": [
            {**a.model_dump(mode="json"), "enabled": True}
            for a in config.payee_accounts
            if (a.tenant_id, a.legal_entity_id) == (tenant, entity)
        ],
    }


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


async def preflight(config: FinanceConfig) -> ReadinessReport:
    """不连接数据库、网关或供应商，不迁移也不启动worker。"""
    resources = await anyio.to_thread.run_sync(credential_resources, config)
    tls = config.environment != "production" or (
        tls_origin(config.gateway.base_url)
        and (config.objects.kind != "s3" or tls_origin(config.objects.endpoint_url or ""))
    )
    resources.append(check("transport_tls", tls, "正式网关与对象端点必须使用TLS"))
    resources.append(
        check(
            "gateway_credentials",
            {c.tenant_id for c in config.companies} <= set(config.gateway.tenant_token_files),
            "每个公司租户须有独立网关凭据",
        )
    )
    scopes = [(c.tenant_id, c.legal_entity_id) for c in config.companies] or [("", "")]
    reports = [build_report(offline_snapshot(config, *scope), resources) for scope in scopes]
    result = reports[0]
    if len(reports) > 1:
        result["checks"] = [c for r in reports for c in r["checks"]]
        result["configuration_digest"] = request_digest(
            [r["configuration_digest"] for r in reports]
        )
        if any(r["status"] == "blocked" for r in reports):
            result["status"] = "blocked"
    return result
