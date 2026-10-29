"""F11正式内部启动必须先拒绝坏资源，并仅消费一次已验快照。"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from xuanpu_finance import runtime
from xuanpu_finance.config import FinanceConfig
from xuanpu_finance.errors import FinanceError

from .test_f6_preflight import configured
from .test_f9_profile import internal_settings

if TYPE_CHECKING:
    from pathlib import Path


def production_config(directory: Path) -> FinanceConfig:
    """组合已有真实PEM及内部制度，固定凭据均为私有文件。"""
    directory.mkdir(exist_ok=True)
    base = configured(directory).model_dump()
    internal = internal_settings()
    for name in ("profile", "companies", "grants", "expense_policies"):
        base[name] = internal[name]
    base["gateway"]["base_url"] = "https://platform.example"
    base["gateway"]["tenant_token_files"] = {"t": directory / "gateway"}
    base["objects"]["root"] = directory / "objects"
    return FinanceConfig.model_validate(base)


def corrupt_file(config: FinanceConfig, failure: str, directory: Path) -> None:
    """私有文件权限、类型和文本读取分别覆盖失败分支。"""
    path = config.service_token_file
    if failure == "permissions":
        path.chmod(0o640)
    elif failure == "symlink":
        link = directory / "link"
        link.symlink_to(path)
        config.service_token_file = link
    elif failure == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif failure == "directory":
        config.service_token_file = directory
    elif failure == "oversize":
        path.write_bytes(b"x" * 65537)
    elif failure == "empty":
        path.write_bytes(b" \n\t")
    elif failure == "utf8":
        path.write_bytes(b"\xff")


def corrupt_resource(config: FinanceConfig, failure: str, directory: Path) -> None:
    """密钥格式、令牌独立性、公司映射和配置语义分别失败。"""
    if failure == "aes":
        config.objects.key_file.write_bytes(b"k" * 31)
    elif failure == "public":
        config.public_key_files["k"].write_bytes(b"invalid-private-pem")
    elif failure == "same_inode":
        gateway = config.gateway.tenant_token_files["t"]
        gateway.unlink()
        gateway.hardlink_to(config.service_token_file)
    elif failure == "same_bytes":
        config.gateway.tenant_token_files["t"].write_bytes(b" \nservice-secret\t")
    elif failure == "dsn":
        config.database_dsn_file.write_bytes(b"postgresql://secret@localhost/finance?host=private")
    elif failure == "tls":
        config.gateway.base_url = "http://private.example"
    elif failure == "gateway":
        config.gateway.tenant_token_files = {}
    elif failure == "configuration":
        config.issuer = "REPLACE_PRIVATE_ISSUER"


def corrupt_s3(config: FinanceConfig, failure: str, directory: Path) -> None:
    """S3必须显式固定凭据和合法TLS端点，不能读取默认云凭据。"""
    config.objects.kind = "s3"
    config.objects.root = None
    config.objects.bucket = "private-bucket"
    config.objects.endpoint_url = "https://objects.example"
    for name in ("access_key_file", "secret_key_file"):
        credential = directory / name
        credential.write_bytes(b"s3-private-credential")
        credential.chmod(0o600)
        setattr(config.objects, name, credential)
    if failure == "s3_missing":
        config.objects.access_key_file = None
    elif failure == "s3_empty":
        assert config.objects.secret_key_file is not None
        config.objects.secret_key_file.write_bytes(b" \n")
    elif failure == "s3_tls":
        config.objects.endpoint_url = "http://private.example"
    elif failure == "s3_port":
        config.objects.endpoint_url = "https://objects.example:99999"


def corrupt(config: FinanceConfig, failure: str, directory: Path) -> None:
    """每种失败只修改一个安全条件，不引入实际网络资源。"""
    if failure.startswith("s3"):
        corrupt_s3(config, failure, directory)
    elif failure in {"permissions", "symlink", "fifo", "directory", "oversize", "empty", "utf8"}:
        corrupt_file(config, failure, directory)
    else:
        corrupt_resource(config, failure, directory)


@pytest.mark.parametrize("failure", [
    "permissions", "symlink", "fifo", "directory", "oversize", "empty", "utf8", "aes",
    "public", "same_inode", "same_bytes", "dsn", "tls", "gateway", "configuration",
    "s3_missing", "s3_empty", "s3_tls", "s3_port",
])
async def test_production_internal_gate_precedes_every_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """结构、文件与TLS失败全部先于第一个连接或客户端构造并固定脱敏。"""
    config = production_config(tmp_path)
    corrupt(config, failure, tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        """坏资源不能通过先连接再拒绝伪装启动门禁。"""
        pytest.fail("side effect reached before credential gate")

    monkeypatch.setattr(runtime.asyncpg, "create_pool", forbidden)
    monkeypatch.setattr(runtime.asyncpg, "connect", forbidden)
    monkeypatch.setattr(runtime.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(runtime, "objects_from_config", forbidden)
    monkeypatch.setattr(runtime, "FinanceWorker", forbidden)
    with pytest.raises(FinanceError) as caught:
        async with runtime.open_runtime(config):
            pytest.fail("unsafe production instance started")
    assert caught.value.code == "INTERNAL_STARTUP_BLOCKED"
    assert str(caught.value) == "INTERNAL_STARTUP_BLOCKED: 内部财务启动检查未通过"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("profile,environment", [
    ("general", "production"), ("internal_records", "test"), ("internal_records", "development"),
])
async def test_other_profiles_keep_explicit_runtime_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, environment: str,
) -> None:
    """普通实例及操作员显式测试环境仍按原有配置进入资源装配。"""
    config = production_config(tmp_path)
    config.profile = profile  # type: ignore[assignment]
    config.environment = environment  # type: ignore[assignment]
    config.service_token_file.chmod(0o644)

    async def reached(*args: Any, **kwargs: Any) -> Any:
        """哨兵只证明原有连接入口仍可达，不连接任何数据库。"""
        raise LookupError("connection boundary reached")

    monkeypatch.setattr(runtime.asyncpg, "create_pool", reached)
    with pytest.raises(LookupError, match="connection boundary reached"):
        async with runtime.open_runtime(config):
            pytest.fail("sentinel bypassed")


def write_cli_config(config: FinanceConfig, directory: Path) -> Path:
    """写可由真实命令解析的配置，不经过测试替换入口。"""
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return path


@pytest.mark.parametrize("failure", ["permissions", "configuration", "s3_missing"])
@pytest.mark.parametrize("explicit_environment", [True, False])
def test_real_cli_blocks_without_traceback_or_secrets(
    tmp_path: Path, failure: str, explicit_environment: bool,
) -> None:
    """实际子进程同时覆盖运行资源错误及load_config结构错误的固定退出契约。"""
    config = production_config(tmp_path)
    corrupt(config, failure, tmp_path)
    path = write_cli_config(config, tmp_path)
    if not explicit_environment:
        raw = yaml.safe_load(path.read_text())
        raw.pop("environment")
        path.write_text(yaml.safe_dump(raw))
    result = subprocess.run(
        [sys.executable, "-m", "xuanpu_finance", "--config", str(path)],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "blocked", "production_status": "not_verified", "code": "INTERNAL_STARTUP_BLOCKED",
    }
    for secret in (str(tmp_path), "private", "REPLACE_", "postgresql", "Traceback"):
        assert secret not in result.stdout + result.stderr


async def test_snapshot_is_immutable_and_has_no_sensitive_repr(tmp_path: Path) -> None:
    """不可变快照与元组映射均不把秘密写入repr。"""
    assert importlib.util.find_spec("xuanpu_finance.startup"), "missing F11 startup gate"
    startup = importlib.import_module("xuanpu_finance.startup")
    _, snapshot = await startup.prepare_startup(production_config(tmp_path))
    assert snapshot is not None
    assert repr(snapshot) == "CredentialSnapshot()"
    with pytest.raises(FrozenInstanceError):
        snapshot.dsn = "private-replacement"
    assert isinstance(snapshot.gateway_tokens, tuple)
    assert isinstance(snapshot.public_keys, tuple)


@pytest.mark.parametrize("storage", ["local", "s3"])
async def test_runtime_consumes_only_validated_snapshot_after_files_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str,
) -> None:
    """池和锁复用DSN，所有认证、网关、AES与S3参数仅来自首次已验字节。"""
    from .f11_startup_helpers import RuntimeSpies

    config = production_config(tmp_path)
    if storage == "s3":
        corrupt(config, "s3_valid", tmp_path)
    public = config.public_key_files["k"].read_text().strip()
    dsn = config.database_dsn_file.read_text().strip()
    spies = RuntimeSpies(config)
    spies.install(monkeypatch)
    async with runtime.open_runtime(config) as app:
        assert spies.seen["pool_dsn"] == spies.seen["lock_dsn"] == dsn
        assert spies.seen["gateway_tokens"] == {"t": "gateway-secret"}
        assert spies.seen["auth"]["service_token"] == "service-secret"
        assert spies.seen["auth"]["public_keys"] == {"k": public}
        if storage == "local":
            assert spies.seen["aes"] == b"k" * 32
        else:
            assert spies.seen["s3"]["aws_access_key_id"] == "s3-private-credential"
            assert spies.seen["s3"]["aws_secret_access_key"] == "s3-private-credential"
            from xuanpu_finance.objects import _Envelope

            encrypted = _Envelope(b"k" * 32).encrypt("tenant/original", b"original-pdf-bytes")
            assert app.state.finance_objects.envelope.decrypt(
                "tenant/original", encrypted
            ) == b"original-pdf-bytes"
    assert not config.objects.root or not config.objects.root.exists()


@pytest.mark.parametrize("error", [
    FinanceError("UNRELATED_ERROR", 409, "其他资源失败"), ValueError("unrelated failure"),
])
def test_cli_only_catches_the_specific_startup_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """正式门禁之外的错误保持真实失败，不被伪装成资源检查结果。"""
    from xuanpu_finance import __main__ as cli

    config = production_config(tmp_path)
    monkeypatch.setattr(cli, "load_startup_config", lambda _: config)
    monkeypatch.setattr("sys.argv", ["finance", "--config", "unused"])

    async def broken(_: Any) -> None:
        """模拟不属于门禁的真实资源错误。"""
        raise error

    monkeypatch.setattr(cli, "serve", broken)
    with pytest.raises(type(error)) as caught:
        cli.main()
    assert caught.value is error
    assert capsys.readouterr().out == ""


def test_check_config_remains_structure_only(tmp_path: Path) -> None:
    """结构检查不会读取凭据或伪称可正式启动。"""
    config = production_config(tmp_path)
    config.service_token_file.unlink()
    result = subprocess.run(
        [sys.executable, "-m", "xuanpu_finance", "--config",
         str(write_cli_config(config, tmp_path)), "--check-config"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert "尚未连接外部资源" in result.stdout
