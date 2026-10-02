"""样例与文档不能悄悄过期：样例要一直通过校验，文档里的站内链接要一直指得到东西。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import get_args

import pytest
import taifeng
from pydantic import BaseModel
from xuanpu_cli.main import build_parser
from xuanpu_sdk import XuanpuClient
from xuanpu_sdk.client import Admin, Packages, Secrets, Sessions
from xuanpu_server.config import ServerConfig, load_config
from xuanpu_spec import (
    PackageManifest,
    load_openapi,
    pack_package,
    unpack_package,
    validate_package,
)

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "examples"
SUPPORT_DESK = EXAMPLES / "support-desk"

# 配置示例里引用到的变量；取值只要形状合法
_VARIABLES = {
    "XUANPU_ROOT_KEY_HASH": "0" * 64,
    "XUANPU_METRICS_TOKEN_HASH": "1" * 64,
    "XUANPU_MASTER_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "XUANPU_PG_DSN": "postgresql://xuanpu@localhost/xuanpu",
    "MODEL_GATEWAY_URL": "http://localhost:4000/v1",
    "MODEL_GATEWAY_KEY": "sk-test",
    "MODEL_NAME": "some-model",
}


def test_example_package_is_valid() -> None:
    """样例业务包通过校验，而且一条提醒都没有——它是给人照着写的。"""
    report = validate_package(SUPPORT_DESK)
    assert report.errors == ()
    assert report.warnings == ()
    assert report.ok


def test_example_package_packs_and_unpacks(tmp_path: Path) -> None:
    """样例业务包能打包，解出来的内容仍然通过校验。"""
    archive = pack_package(SUPPORT_DESK)
    unpack_package(archive, tmp_path / "unpacked")
    assert validate_package(tmp_path / "unpacked").ok


async def test_example_skills_load_in_kernel() -> None:
    """内核认这些 skill：入口可见的工具正是文档里说的那几个。"""
    registry = await taifeng.FilesystemSkillRegistry.load(SUPPORT_DESK / "skills")
    triage = registry.snapshot().get("triage")
    assert triage is not None
    assert triage.visible_tool_names() == {
        "api__tickets__listTickets",
        "api__tickets__getTicket",
        "api__tickets__updateTicket",
        "call_skill",
        "read_skill",
        "run_script",
    }


def test_example_openapi_matches_whitelist() -> None:
    """清单白名单里的 operation 都在 OpenAPI 文档里，只读与否和文档里写的一致。"""
    document = load_openapi(SUPPORT_DESK / "apis" / "tickets.yaml")
    assert set(document.operations) == {"listTickets", "getTicket", "updateTicket"}
    assert document.operations["getTicket"].read_only
    assert not document.operations["updateTicket"].read_only


@pytest.mark.parametrize("name", ["xuanpu.yaml", "xuanpu.trial.yaml"])
def test_example_configs_parse(name: str) -> None:
    """两份配置示例都是合法配置。"""
    config = load_config(EXAMPLES / name, variables=_VARIABLES)
    assert set(config.models.tiers) == {"fast", "balanced", "deep"}


# --- 文档里的站内链接 ---

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _documents() -> list[Path]:
    """仓库里的全部 Markdown 文档。"""
    found = [REPO / "README.md", REPO / "AGENTS.md"]
    found += sorted((REPO / "docs").rglob("*.md"))
    found += sorted((REPO / "packages").glob("*/README.md"))
    found += sorted(EXAMPLES.rglob("*.md"))
    return found


def _anchors(path: Path) -> set[str]:
    """一份文档里各级标题对应的锚点（与代码托管平台的生成规则一致）。"""
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    for heading in _HEADING.findall(text):
        plain = re.sub(r"[`*]", "", heading).strip().lower()
        anchors.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", plain)))
    return anchors


def _broken_links(path: Path) -> list[str]:
    """一份文档里指不到东西的站内链接。"""
    broken: list[str] = []
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        if not resolved.exists():
            broken.append(f"{target}（文件不存在）")
        elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            broken.append(f"{target}（没有这个标题）")
    return broken


@pytest.mark.parametrize("path", _documents(), ids=lambda path: str(path.relative_to(REPO)))
def test_document_links_resolve(path: Path) -> None:
    """文档里的相对链接都指向存在的文件与标题。"""
    assert _broken_links(path) == []


# --- 文档覆盖：代码里有的，参考文档里也要有 ---

SERVER_SRC = REPO / "packages" / "xuanpu-server" / "src" / "xuanpu_server"


def _read(relative: str) -> str:
    """读仓库里的一份文档。"""
    return (REPO / relative).read_text(encoding="utf-8")


def _field_names(model: type[BaseModel]) -> set[str]:
    """一个 pydantic 模型及其嵌套模型的全部字段名（有别名用别名）。"""
    names: set[str] = set()
    pending: list[type[BaseModel]] = [model]
    seen: set[type[BaseModel]] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for name, field in current.model_fields.items():
            names.add(field.alias or name)
            pending.extend(_nested_models(field.annotation))
    return names


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    """类型标注里出现的 pydantic 模型。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        found.extend(_nested_models(argument))
    return found


def _missing(names: set[str], document: str) -> list[str]:
    """哪些名字没有作为完整的词出现在文档里。"""
    return sorted(
        name
        for name in names
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", document)
    )


def test_every_route_is_documented() -> None:
    """HTTP 接口文档覆盖全部路由。"""
    source = (SERVER_SRC / "api" / "app.py").read_text(encoding="utf-8")
    routes = re.findall(r'@app\.(get|post|put|delete)\("([^"]+)"', source)
    assert len(routes) >= 25
    document = re.sub(r"\{[a-z_]+\}", "{}", _read("docs/architecture/http-api.md"))
    missing = [
        f"{method.upper()} {path}"
        for method, path in routes
        if f"{method.upper()} {re.sub(r'\{[a-z_]+\}', '{}', path)}" not in document
    ]
    assert missing == []


def test_every_error_code_is_documented() -> None:
    """HTTP 接口文档覆盖全部错误码。"""
    codes = set(re.findall(r'^\s+code = "([a-z_]+)"', _read_source("errors.py"), re.MULTILINE))
    codes |= set(re.findall(r'^\s+code = "([a-z_]+)"', _read_source("secrets.py"), re.MULTILINE))
    assert "session_busy" in codes
    assert _missing(codes, _read("docs/architecture/http-api.md")) == []


def _read_source(relative: str) -> str:
    """读平台服务的一个源文件。"""
    return (SERVER_SRC / relative).read_text(encoding="utf-8")


def test_every_config_key_is_documented() -> None:
    """运维手册覆盖全部配置项，配置示例也都给出了（哪怕是注释掉的）。"""
    keys = _field_names(ServerConfig)
    assert {"data_dir", "schema", "session_idle_seconds", "docker_env"} <= keys
    assert _missing(keys, _read("docs/guides/operations.md")) == []
    assert _missing(keys, _read("examples/xuanpu.yaml")) == []


def test_every_manifest_field_is_documented() -> None:
    """业务包规范覆盖清单的全部字段。"""
    fields = _field_names(PackageManifest)
    assert {"apiVersion", "notContains", "timeoutSeconds", "baseUrl"} <= fields
    assert _missing(fields, _read("docs/architecture/package-spec.md")) == []


def test_every_audit_kind_is_documented() -> None:
    """审计手册覆盖平台自己写入的全部事件类型。"""
    kinds: set[str] = {"connector_call", "turn_text", "audit_gap", "usage_estimated"}
    for path in SERVER_SRC.rglob("*.py"):
        kinds |= set(
            re.findall(
                r'\.control\(\s*\w+,\s*[\w.]+,\s*"([a-z_]+)"', path.read_text(encoding="utf-8")
            )
        )
    assert {"message_sent", "suspension_resolved_by", "tenant_created"} <= kinds
    assert _missing(kinds, _read("docs/guides/audit-and-usage.md")) == []


def test_every_cli_command_and_option_is_documented() -> None:
    """命令行说明覆盖全部子命令与选项。"""
    document = _read("packages/xuanpu-cli/README.md")
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    missing: list[str] = []
    for name, command in subparsers.choices.items():
        if f"xuanpu {name}" not in document:
            missing.append(name)
        for action in command._actions:
            for option in action.option_strings:
                if option.startswith("--") and option != "--help" and option not in document:
                    missing.append(f"{name} {option}")
    assert missing == []


def test_every_sdk_method_is_documented() -> None:
    """Python 客户端说明覆盖全部公开方法。"""
    document = _read("packages/xuanpu-sdk-py/README.md")
    methods = {
        name
        for group in (XuanpuClient, Sessions, Packages, Secrets, Admin)
        for name, member in vars(group).items()
        if callable(member) and not name.startswith("_")
    }
    assert {"ask", "replay", "evaluate", "issue_tenant_key", "health"} <= methods
    assert _missing(methods, document) == []
