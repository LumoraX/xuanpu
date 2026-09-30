"""命令行。"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from typing import TYPE_CHECKING

import pytest
from xuanpu_cli.main import main

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST = (
    "apiVersion: xuanpu/v1\n"
    "metadata: {name: tiny, version: 1.0.0}\n"
    "spec:\n"
    "  skills: {entries: [main]}\n"
    "  evals:\n"
    "    - {name: greet, entry: main, input: 你好}\n"
)
MAIN = (
    "---\nname: main\ndescription: d\nversion: 1.0.0\ntype: composite\nentry: true\n"
    "child_skills: [helper]\n---\n正文\n"
)
HELPER = "---\nname: helper\ndescription: d\nversion: 1.0.0\ntype: atomic\n---\n正文\n"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """一个合法的业务包目录。"""
    root = tmp_path / "pkg"
    (root / "skills" / "main").mkdir(parents=True)
    (root / "skills" / "helper").mkdir(parents=True)
    (root / "xuanpu.yaml").write_text(MANIFEST, encoding="utf-8")
    (root / "skills" / "main" / "SKILL.md").write_text(MAIN, encoding="utf-8")
    (root / "skills" / "helper" / "SKILL.md").write_text(HELPER, encoding="utf-8")
    return root


def test_validate_ok(package: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """合法的业务包：退出码 0。"""
    assert main(["validate", str(package)]) == 0
    assert "tiny 1.0.0：校验通过" in capsys.readouterr().out


def test_validate_reports_errors_and_warnings(
    package: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """有问题的业务包：错误写到标准错误，提醒写到标准输出，退出码非 0。"""
    (package / "xuanpu.yaml").write_text(
        MANIFEST.replace("entries: [main]", "entries: [main, ghost]").replace(
            "  evals:\n    - {name: greet, entry: main, input: 你好}\n", ""
        )
    )
    assert main(["validate", str(package)]) == 1
    captured = capsys.readouterr()
    assert "错误：入口 skill 'ghost'" in captured.err
    assert "提醒：没有评测用例" in captured.out
    assert "校验通过" not in captured.out


def test_validate_missing_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """目录不存在。"""
    assert main(["validate", str(tmp_path / "nope")]) == 1
    assert "读不到清单" in capsys.readouterr().err


def test_pack(package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """打包：先校验，再写出归档；输出里有指纹。"""
    output = tmp_path / "tiny.zip"
    assert main(["pack", str(package), "-o", str(output)]) == 0
    assert "sha256:" in capsys.readouterr().out
    with zipfile.ZipFile(io.BytesIO(output.read_bytes())) as archive:
        assert sorted(archive.namelist()) == [
            "skills/helper/SKILL.md",
            "skills/main/SKILL.md",
            "xuanpu.yaml",
        ]


def test_pack_refuses_invalid_package(package: Path, tmp_path: Path) -> None:
    """校验不过的业务包不打包。"""
    (package / "skills" / "main" / "SKILL.md").unlink()
    output = tmp_path / "tiny.zip"
    assert main(["pack", str(package), "-o", str(output)]) == 1
    assert not output.exists()


def test_schema(capsys: pytest.CaptureFixture[str]) -> None:
    """打印清单的 JSON Schema。"""
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "apiVersion" in schema["properties"]


def test_keygen_root_matches_platform_verification(capsys: pytest.CaptureFixture[str]) -> None:
    """生成的根凭据与摘要能对上。"""
    from xuanpu_server.tenancy import verify_root_key

    assert main(["keygen", "root"]) == 0
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    key = next(line for line in lines if line.startswith("xproot_"))
    digest = lines[-1]
    assert verify_root_key(key, digest)
    assert not verify_root_key(key + "x", digest)


def test_keygen_master(capsys: pytest.CaptureFixture[str]) -> None:
    """生成的主密钥是 32 字节。"""
    assert main(["keygen", "master"]) == 0
    encoded = capsys.readouterr().out.splitlines()[-1].strip()
    assert len(base64.b64decode(encoded, validate=True)) == 32


def test_config_errors_are_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """配置有问题：说清楚原因，退出码非 0。"""
    assert main(["serve", "--config", str(tmp_path / "missing.yaml")]) == 1
    assert "读不到配置文件" in capsys.readouterr().err
    broken = tmp_path / "broken.yaml"
    broken.write_text("data_dir: relative\n")
    assert main(["migrate", "--config", str(broken)]) == 1
    assert "配置不合法" in capsys.readouterr().err


def test_migrate_with_memory_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """控制面用内存存储时没有要迁移的表；配置里的变量取自进程环境。"""
    monkeypatch.setenv("GATEWAY_KEY", "sk-x")
    config = tmp_path / "xuanpu.yaml"
    config.write_text(
        json.dumps(
            {
                "data_dir": str(tmp_path / "data"),
                "root_key_hash": "a" * 64,
                "master_keys": {"k1": base64.b64encode(b"k" * 32).decode()},
                "primary_master_key": "k1",
                "models": {
                    "base_url": "http://gateway/v1",
                    "api_key": "${GATEWAY_KEY}",
                    "tiers": {
                        "fast": {"model": "s"},
                        "balanced": {"model": "m"},
                        "deep": {"model": "l"},
                    },
                },
            }
        )
    )
    assert main(["migrate", "--config", str(config)]) == 0
    assert "没有需要迁移的表" in capsys.readouterr().out


def test_unknown_command_is_usage_error() -> None:
    """未知命令按用法错误退出。"""
    with pytest.raises(SystemExit) as caught:
        main(["launch-rockets"])
    assert caught.value.code == 2
