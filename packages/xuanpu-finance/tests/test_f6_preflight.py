"""F6离线检查必须有界、脱敏且绝不进入运行资源。"""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.config import FinanceConfig

from .test_config import settings

if TYPE_CHECKING:
    from pathlib import Path


def configured(tmp_path: Path) -> FinanceConfig:
    """生成仅供离线检查的独立固定文件。"""
    config = FinanceConfig.model_validate(
        settings(
            companies=[{"tenant_id": "acme", "legal_entity_id": "e", "name": "公司", "tax_id": "T"}]
        )
    )
    files = {
        "database_dsn_file": b"postgresql://finance:private@localhost/finance",
        "service_token_file": b"service-secret",
        "public": Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
        "gateway": b"gateway-secret",
        "aes": b"k" * 32,
    }
    paths = {}
    for key, data in files.items():
        path = tmp_path / key
        path.write_bytes(data)
        path.chmod(0o600)
        paths[key] = path
    config.database_dsn_file = paths["database_dsn_file"]
    config.service_token_file = paths["service_token_file"]
    config.public_key_files = {"k": paths["public"]}
    config.gateway.tenant_token_files = {"acme": paths["gateway"]}
    config.objects.key_file = paths["aes"]
    return config


async def report(config: FinanceConfig) -> dict[str, Any]:
    """缺失入口使用明确断言记录RED。"""
    assert importlib.util.find_spec("xuanpu_finance.readiness"), "缺少F6 readiness包"
    module = importlib.import_module("xuanpu_finance.readiness.offline")
    return await module.preflight(config)


async def test_offline_never_claims_production_and_redacts(tmp_path: Path) -> None:
    """有效资源仍保留正式未验且JSON不含秘密引用。"""
    config = configured(tmp_path)
    result = await report(config)
    assert result["production_status"] == "not_verified"
    assert result["external_verifications"]
    raw = json.dumps(result)
    for secret in ("private", "service-secret", "gateway-secret", str(tmp_path), "postgresql:"):
        assert secret not in raw
    assert all(set(c) == {"code", "status", "detail"} for c in result["checks"])
    assert (
        next(c for c in result["checks"] if c["code"] == "credential_resources")["status"]
        == "passed"
    )


@pytest.mark.parametrize(
    "failure",
    [
        "permissions",
        "reuse_bytes",
        "reuse_path",
        "short_aes",
        "bad_public",
        "bad_dsn",
        "oversize",
        "directory",
        "symlink",
    ],
)
async def test_offline_rejects_unsafe_resource_without_echo(tmp_path: Path, failure: str) -> None:
    """权限、文件类型、格式、长度与共享令牌失败只输出稳定code。"""
    config = configured(tmp_path)
    if failure == "permissions":
        config.service_token_file.chmod(0o640)
    if failure == "reuse_bytes":
        config.gateway.tenant_token_files["acme"].write_bytes(b"service-secret")
    if failure == "reuse_path":
        config.gateway.tenant_token_files["acme"] = config.service_token_file
    if failure == "short_aes":
        config.objects.key_file.write_bytes(b"x")
    if failure == "bad_public":
        config.public_key_files["k"].write_bytes(b"private key invalid")
    if failure == "bad_dsn":
        config.database_dsn_file.write_bytes(b"private invalid dsn")
    if failure == "oversize":
        config.service_token_file.write_bytes(b"x" * 70000)
    if failure == "directory":
        config.service_token_file = tmp_path
    if failure == "symlink":
        link = tmp_path / "link"
        link.symlink_to(config.service_token_file)
        config.service_token_file = link
    result = await report(config)
    assert result["status"] == "blocked"
    assert any(c["status"] == "blocked" for c in result["checks"])
    assert str(tmp_path) not in json.dumps(result)


def test_preflight_cli_load_error_redacted_without_runtime(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """结构错误不会启动资源且异常原文不进入stdout/stderr。"""
    from xuanpu_finance import __main__ as cli

    config = tmp_path / "private-config.yaml"
    config.write_text("private-secret: [", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["finance", "--config", str(config), "--preflight"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "private" not in captured.out + captured.err
    assert json.loads(captured.out)["status"] == "blocked"


def test_preflight_cli_never_enters_runtime_and_flags_exclusive(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """离线结构可加载时仍不能创建服务，互斥选项由解析器拒绝。"""
    from xuanpu_finance import __main__ as cli

    config = configured(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: config)

    def forbidden(_: Any) -> Any:
        """任何运行入口调用都说明离线隔离失效。"""
        raise AssertionError("runtime forbidden")

    monkeypatch.setattr(cli, "serve", forbidden)
    monkeypatch.setattr("sys.argv", ["finance", "--config", "unused", "--preflight"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["production_status"] == "not_verified"
    monkeypatch.setattr(
        "sys.argv", ["finance", "--config", "unused", "--preflight", "--check-config"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


async def test_production_gateway_requires_tls(tmp_path: Path) -> None:
    """正式网关拒绝明文传输。"""
    config = configured(tmp_path)
    config.gateway.base_url = "http://platform.example"
    result = await report(config)
    assert next(c for c in result["checks"] if c["code"] == "transport_tls")["status"] == "blocked"


@pytest.mark.parametrize(
    "address", ["https:", "https://user:password@objects.example", "http://objects.example"]
)
async def test_s3_endpoint_must_be_valid_private_tls_origin(tmp_path: Path, address: str) -> None:
    """S3端点不能只凭scheme通过TLS检查。"""
    config = configured(tmp_path)
    config.objects.kind = "s3"
    config.objects.root = None
    config.objects.endpoint_url = address
    result = await report(config)
    assert next(c for c in result["checks"] if c["code"] == "transport_tls")["status"] == "blocked"


async def test_gateway_token_coverage_required(tmp_path: Path) -> None:
    """已有公司的网关凭据不能因空映射而被判断准备完成。"""
    config = configured(tmp_path)
    config.gateway.tenant_token_files = {}
    result = await report(config)
    assert (
        next(c for c in result["checks"] if c["code"] == "gateway_credentials")["status"]
        == "blocked"
    )


async def test_dsn_query_cannot_override_explicit_endpoint(tmp_path: Path) -> None:
    """禁止驱动查询参数另指向未声明地址。"""
    config = configured(tmp_path)
    config.database_dsn_file.write_bytes(
        b"postgresql://finance:private@localhost/finance?host=remote"
    )
    result = await report(config)
    assert (
        next(c for c in result["checks"] if c["code"] == "credential_resources")["status"]
        == "blocked"
    )


@pytest.mark.parametrize(
    "failure",
    ["callback_short", "provider_reuse_bytes", "provider_reuse_path", "invalid_token_utf8"],
)
async def test_supplier_credentials_boundaries(tmp_path: Path, failure: str) -> None:
    """供应商令牌须独立且可解析，回调密钥须达到32字节。"""
    from xuanpu_finance.settlement.provider_models import ProviderConfig

    config = configured(tmp_path)
    token = tmp_path / "supplier-token"
    token.write_bytes(b"supplier-secret")
    token.chmod(0o600)
    callback = tmp_path / "callback"
    callback.write_bytes(b"c" * 32)
    callback.chmod(0o600)
    provider = ProviderConfig(
        tenant_id="acme",
        legal_entity_id="e",
        provider="supplier",
        configuration_version=1,
        adapter_version="v1",
        base_url="https://supplier.example",
        token_file=token,
        callback_secret_file=callback,
        callback_key_id="k",
    )
    config.settlement_providers = [provider]
    if failure == "callback_short":
        callback.write_bytes(b"c" * 31)
    if failure == "provider_reuse_bytes":
        token.write_bytes(b"service-secret")
    if failure == "provider_reuse_path":
        provider.token_file = config.service_token_file
    if failure == "invalid_token_utf8":
        token.write_bytes(b"\xff")
    result = await report(config)
    assert (
        next(c for c in result["checks"] if c["code"] == "credential_resources")["status"]
        == "blocked"
    )


async def test_malformed_s3_endpoint_is_fixed_failure(tmp_path: Path) -> None:
    """无效IPv6端点不得抛出可能泄露配置的异常。"""
    config = configured(tmp_path)
    config.objects.kind = "s3"
    config.objects.root = None
    config.objects.endpoint_url = "https://[private"
    result = await report(config)
    assert next(c for c in result["checks"] if c["code"] == "transport_tls")["status"] == "blocked"


def test_preflight_cli_unexpected_error_is_redacted(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """检查执行期未知异常也不得通过CLI回显秘密正文。"""
    from xuanpu_finance import __main__ as cli

    monkeypatch.setattr(cli, "load_config", lambda _: configured(tmp_path))

    async def broken(_: Any) -> Any:
        """模拟资源读取之外的故障仍携带秘密。"""
        raise ValueError("private-dsn-secret")

    monkeypatch.setattr(cli, "preflight", broken)
    monkeypatch.setattr("sys.argv", ["finance", "--config", "unused", "--preflight"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "private" not in captured.out + captured.err
    assert json.loads(captured.out)["status"] == "blocked"


@pytest.mark.parametrize("key_name", ["access_key_file", "secret_key_file"])
@pytest.mark.parametrize("value", [b" \n", b"\xff"])
async def test_s3_credentials_require_nonempty_utf8(
    tmp_path: Path, key_name: str, value: bytes
) -> None:
    """S3凭据须与运行时一致地解码UTF8并拒绝空白正文。"""
    config = configured(tmp_path)
    config.objects.kind = "s3"
    config.objects.root = None
    config.objects.bucket = "finance"
    config.objects.endpoint_url = "https://objects.example"
    for name in ("access_key_file", "secret_key_file"):
        path = tmp_path / name
        path.write_bytes(b"valid-credential")
        path.chmod(0o600)
        setattr(config.objects, name, path)
    valid = await report(config)
    assert (
        next(c for c in valid["checks"] if c["code"] == "credential_resources")["status"]
        == "passed"
    )
    getattr(config.objects, key_name).write_bytes(value)
    result = await report(config)
    assert (
        next(c for c in result["checks"] if c["code"] == "credential_resources")["status"]
        == "blocked"
    )
    assert str(tmp_path) not in json.dumps(result)
