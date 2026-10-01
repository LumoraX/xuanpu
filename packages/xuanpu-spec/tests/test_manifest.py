"""清单的解析与字段约束。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from pydantic import ValidationError
from xuanpu_spec import (
    ConnectorSpec,
    ManifestError,
    dump_manifest,
    load_manifest,
    manifest_json_schema,
    parse_manifest,
    version_key,
)

if TYPE_CHECKING:
    from pathlib import Path


def _edited(base: str, **changes: object) -> str:
    """在合法清单的基础上按双下划线路径改字段；值为 ``...`` 表示删除该字段。"""
    data = yaml.safe_load(base)
    for path, value in changes.items():
        *parents, last = path.split("__")
        node = data
        for key in parents:
            node = node[int(key)] if isinstance(node, list) else node[key]
        if value is ...:
            del node[last]
        else:
            node[last] = value
    return yaml.safe_dump(data, allow_unicode=True)


def test_parse_full_manifest(samples: Any) -> None:
    """完整清单解析后各字段可用。"""
    manifest = parse_manifest(samples.manifest)
    assert (manifest.name, manifest.version) == ("support-desk", "1.2.0")
    assert manifest.spec.skills.entries == ("triage",)
    assert manifest.spec.policy.budget.max_session_tokens == 200000
    connector = manifest.spec.connectors[0]
    assert connector.env["CRM_TOKEN"].secret == "crm-token"
    assert connector.env["CRM_REGION"].value == "cn"
    assert manifest.spec.secret_names == ("crm-token",)
    assert manifest.spec.evals[0].expect.max_tokens == 2000


def test_minimal_manifest_gets_conservative_defaults(samples: Any) -> None:
    """只写必填项时，缺省值偏保守：标准审批、不出网、不开放命令执行。"""
    manifest = parse_manifest(
        "apiVersion: xuanpu/v1\n"
        "metadata: {name: tiny, version: 0.1.0}\n"
        "spec: {skills: {entries: [main]}}\n"
    )
    policy = manifest.spec.policy
    assert manifest.kind == "Package"
    assert manifest.spec.skills.dir == "skills"
    assert manifest.spec.model.tier == "balanced"
    assert policy.approval == "standard"
    assert (policy.sandbox.network, policy.sandbox.shell) == (False, False)
    assert manifest.spec.connectors == ()


def test_connector_effect_and_allowlist(samples: Any) -> None:
    """副作用分类：逐工具声明优先；未声明的用连接器默认值。白名单外的工具不放行。"""
    connector = parse_manifest(samples.manifest).spec.connectors[0]
    assert connector.effect_of("search_customer") == "read"
    assert connector.effect_of("update_ticket") == "external"
    assert connector.allows("update_ticket")
    assert not connector.allows("delete_everything")


def test_connector_without_tool_list_allows_all_with_default_effect(samples: Any) -> None:
    """没写工具白名单：全部放行，副作用按最保守的 external。"""
    text = _edited(
        samples.manifest, spec__connectors__0__tools=..., spec__connectors__0__effect=...
    )
    connector = parse_manifest(text).spec.connectors[0]
    assert connector.allows("anything")
    assert connector.effect_of("anything") == "external"


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"metadata__name": "Support_Desk"}, "metadata.name"),
        ({"metadata__name": "-lead"}, "metadata.name"),
        ({"metadata__version": "1.2"}, "metadata.version"),
        ({"metadata__version": "v1.2.0"}, "metadata.version"),
        ({"metadata__version": "01.2.0"}, "metadata.version"),
        ({"spec__skills__entries": []}, "spec.skills.entries"),
        ({"spec__skills__entries": ["a", "a"]}, "重复"),
        ({"spec__skills__dir": "/abs/skills"}, "spec.skills.dir"),
        ({"spec__model__tier": "gpt-4o"}, "spec.model.tier"),
        ({"spec__policy__approval": "none"}, "spec.policy.approval"),
        ({"spec__policy__budget__maxSessionTokens": 10}, "maxSessionTokens"),
        ({"spec__policy__budget__maxIterations": 0}, "maxIterations"),
        ({"spec__connectors__0__command": []}, "command"),
        ({"spec__connectors__0__transport": "carrier-pigeon"}, "transport"),
        ({"spec__connectors__0__effect": "harmless"}, "effect"),
        ({"spec__connectors__0__env": {"1BAD": {"value": "x"}}}, "env"),
        ({"spec__connectors__0__env": {"K": {"value": "x", "secret": "s"}}}, "只能给一个"),
        ({"spec__connectors__0__env": {"K": {}}}, "只能给一个"),
        ({"spec__connectors__0__env": {"K": {"secret": "Bad Name"}}}, "secret"),
        ({"spec__connectors__0__env": {"LD_PRELOAD": {"value": "/x.so"}}}, "LD_PRELOAD"),
        ({"spec__connectors__0__env": {"ld_library_path": {"value": "/x"}}}, "动态链接器"),
        ({"spec__connectors__0__env": {"DYLD_INSERT_LIBRARIES": {"value": "/x"}}}, "DYLD_"),
        ({"spec__connectors__0__env": {"Dyld_Fallback_Library_Path": {"value": "/x"}}}, "沙盒外"),
        ({"spec__evals__0__entry": "not-an-entry"}, "不在 entries"),
        ({"spec__evals__0__input": ""}, "input"),
    ],
)
def test_invalid_fields_rejected(changes: dict[str, object], fragment: str, samples: Any) -> None:
    """不合法的取值被拒绝，错误信息指到具体字段。"""
    with pytest.raises(ManifestError) as caught:
        parse_manifest(_edited(samples.manifest, **changes))
    assert fragment in str(caught.value)


def test_loader_variable_check_only_looks_at_the_prefix(samples: Any) -> None:
    """只拦以 LD_ / DYLD_ 开头的名字；名字中间含有这几个字母的照常放行。"""
    env = {name: {"value": "x"} for name in ("OLD_PATH", "WORLD_LD_X", "LDAP_URL", "XDYLD_Y")}
    manifest = parse_manifest(_edited(samples.manifest, spec__connectors__0__env=env))
    assert set(manifest.spec.connectors[0].env) == set(env)


@pytest.mark.parametrize(
    "path",
    ["surprise", "metadata__owner", "spec__policy__sandbox__privileged", "spec__model__name"],
)
def test_unknown_fields_rejected(path: str, samples: Any) -> None:
    """多余的字段一律拒绝：拼错的字段名不能被悄悄忽略。"""
    with pytest.raises(ManifestError, match=path.rsplit("__", 1)[-1]):
        parse_manifest(_edited(samples.manifest, **{path: "x"}))


def test_duplicate_connectors_and_evals_rejected(samples: Any) -> None:
    """连接器与评测用例不能重名。"""
    data = yaml.safe_load(samples.manifest)
    data["spec"]["connectors"].append(data["spec"]["connectors"][0])
    with pytest.raises(ManifestError, match="重名"):
        parse_manifest(yaml.safe_dump(data))
    data = yaml.safe_load(samples.manifest)
    data["spec"]["evals"].append(data["spec"]["evals"][0])
    with pytest.raises(ManifestError, match="重名"):
        parse_manifest(yaml.safe_dump(data))


@pytest.mark.parametrize("version", [None, "xuanpu/v2", "v1", 1])
def test_unsupported_api_version(version: object, samples: Any) -> None:
    """apiVersion 不对直接拒绝，不尝试按当前版本硬解。"""
    changes: dict[str, object] = {"apiVersion": ... if version is None else version}
    with pytest.raises(ManifestError, match="apiVersion"):
        parse_manifest(_edited(samples.manifest, **changes))


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string", "a: [unclosed", "\t: x"])
def test_malformed_documents(text: str, samples: Any) -> None:
    """不是映射、不是合法 YAML 的输入被拒绝。"""
    with pytest.raises(ManifestError):
        parse_manifest(text)


def test_yaml_cannot_construct_objects(samples: Any) -> None:
    """清单是不可信输入：YAML 标签不能用来构造对象。"""
    with pytest.raises(ManifestError, match="YAML"):
        parse_manifest("apiVersion: !!python/object/apply:os.system ['true']\n")


def test_oversized_manifest_rejected(samples: Any) -> None:
    """超大的清单被拒绝。"""
    with pytest.raises(ManifestError, match="超过"):
        parse_manifest(samples.manifest + "# " + "x" * 300_000)


def test_load_from_file(tmp_path: Path, samples: Any) -> None:
    """从文件读取；文件不存在或编码不对时报清单错误。"""
    path = tmp_path / "xuanpu.yaml"
    path.write_text(samples.manifest, encoding="utf-8")
    assert load_manifest(path).name == "support-desk"
    with pytest.raises(ManifestError, match="读不到"):
        load_manifest(tmp_path / "missing.yaml")
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ManifestError, match="UTF-8"):
        load_manifest(path)


def test_dump_roundtrip(samples: Any) -> None:
    """写回 YAML 再解析得到同一份清单；字段用对外的驼峰名。"""
    manifest = parse_manifest(samples.manifest)
    dumped = dump_manifest(manifest)
    assert "maxSessionTokens" in dumped
    assert "max_session_tokens" not in dumped
    assert parse_manifest(dumped) == manifest


def test_manifest_is_immutable(samples: Any) -> None:
    """清单解析后不可变。"""
    manifest = parse_manifest(samples.manifest)
    with pytest.raises(Exception, match="frozen"):
        manifest.metadata.name = "other"  # type: ignore[misc]


def test_json_schema_uses_public_field_names(samples: Any) -> None:
    """JSON Schema 用对外字段名，且禁止多余字段。"""
    schema = manifest_json_schema()
    assert "apiVersion" in schema["properties"]
    assert schema["additionalProperties"] is False
    assert "maxSessionTokens" in schema["$defs"]["BudgetSpec"]["properties"]


def test_remote_connector_shape() -> None:
    """remote 连接器：工具名前缀与 stdio 一样是 mcp__，凭据只引用密钥名。"""
    spec = ConnectorSpec.model_validate(
        {
            "name": "docs",
            "transport": "remote",
            "url": "https://mcp.example.com/mcp",
            "auth": {"type": "bearer", "secret": "docs-token"},
        }
    )
    assert spec.tool_name("search") == "mcp__docs__search"
    assert spec.secret_names == ("docs-token",)
    assert spec.timeout_seconds == 30.0
    assert spec.effect_of("search") == "external"


def test_remote_connector_optional_fields() -> None:
    """remote 连接器可以不要凭据；试用时可以用 http://；可以设单次调用的超时与 header 凭据。"""
    plain = ConnectorSpec.model_validate(
        {"name": "docs", "transport": "remote", "url": "http://127.0.0.1:8080/mcp"}
    )
    assert (plain.auth, plain.secret_names) == (None, ())
    keyed = ConnectorSpec.model_validate(
        {
            "name": "docs",
            "transport": "remote",
            "url": "https://mcp.example.com/mcp",
            "timeoutSeconds": 5,
            "auth": {"type": "header", "secret": "docs-key", "header": "X-Api-Key"},
        }
    )
    assert (keyed.timeout_seconds, keyed.secret_names) == (5.0, ("docs-key",))


@pytest.mark.parametrize(
    "extra",
    [
        {"command": ["x"]},
        {"openapi": "a.yaml"},
        {"env": {"A": {"value": "1"}}},
        {"baseUrl": "https://x"},
    ],
)
def test_remote_connector_rejects_other_transport_fields(extra: dict[str, Any]) -> None:
    """remote 连接器不能带别的传输方式的字段：command / env / openapi / baseUrl 一律拒绝。"""
    with pytest.raises(ValidationError):
        ConnectorSpec.model_validate(
            {"name": "d", "transport": "remote", "url": "https://x/mcp", **extra}
        )


def test_remote_connector_needs_an_http_url() -> None:
    """remote 连接器必须给 url，且是带主机名的 http:// 或 https:// 地址。"""
    for url in (None, "ftp://x", "mcp.example.com", "https://", "https:///mcp"):
        with pytest.raises(ValidationError):
            ConnectorSpec.model_validate({"name": "d", "transport": "remote", "url": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example.com:abc/mcp",
        "https://mcp.example.com:99999/mcp",
        "https://exa mple.com/mcp",
        "https://mcp.example.com/m cp",
        "https://mcp.example.com/mcp\t",
        "https://mcp.example.com/\x01mcp",
        "https://mcp\u200b.example.com/mcp",
        "https://[::1/mcp",
    ],
)
def test_remote_url_must_parse_cleanly(url: str) -> None:
    """url 的端口必须是合法数字；不能有空白、控制字符与不可见字符；方括号要配对。"""
    with pytest.raises(ValidationError, match="url"):
        ConnectorSpec.model_validate({"name": "d", "transport": "remote", "url": url})


def test_remote_url_with_port_and_query_is_accepted() -> None:
    """带端口与查询参数的地址是合法的（查询参数在包级校验里另给提醒）。"""
    spec = ConnectorSpec.model_validate(
        {"name": "d", "transport": "remote", "url": "https://mcp.example.com:8443/mcp?tenant=a"}
    )
    assert spec.url == "https://mcp.example.com:8443/mcp?tenant=a"


RESERVED_HEADERS = [
    "Host",
    "content-type",
    "Content-Length",
    "ACCEPT",
    "Transfer-Encoding",
    "connection",
    "Keep-Alive",
    "Proxy-Connection",
    "TE",
    "Trailer",
    "Upgrade",
    "Mcp-Session-Id",
    "MCP-Protocol-Version",
    "last-event-id",
]


@pytest.mark.parametrize("header", RESERVED_HEADERS)
@pytest.mark.parametrize(
    "base",
    [
        {"name": "d", "transport": "remote", "url": "https://x/mcp"},
        {"name": "d", "transport": "http", "openapi": "a.yaml"},
    ],
    ids=["remote", "http"],
)
def test_auth_header_cannot_be_a_protocol_header(base: dict[str, Any], header: str) -> None:
    """auth 的 header 不能是协议、连接管理或 MCP 传输自己用的头（不分大小写），两种传输都一样。"""
    auth = {"type": "header", "secret": "k", "header": header}
    with pytest.raises(ValidationError, match="header 不能是"):
        ConnectorSpec.model_validate({**base, "auth": auth})


def test_ordinary_auth_headers_are_accepted() -> None:
    """普通的自定义头照常可用。"""
    for header in ("X-Api-Key", "Api-Key", "X-Host-Token"):
        spec = ConnectorSpec.model_validate(
            {
                "name": "d",
                "transport": "remote",
                "url": "https://x/mcp",
                "auth": {"type": "header", "secret": "k", "header": header},
            }
        )
        assert spec.auth is not None and spec.auth.header == header


@pytest.mark.parametrize(
    "url", ["https://user:pass@mcp.example.com/mcp", "https://token@mcp.example.com/mcp"]
)
def test_remote_url_cannot_carry_credentials(url: str) -> None:
    """url 里不能带用户名或口令：凭据只能写进 auth，并且只写密钥名。"""
    with pytest.raises(ValidationError, match="auth"):
        ConnectorSpec.model_validate({"name": "d", "transport": "remote", "url": url})


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "query", "secret": "docs-token", "name": "key"},
        {"type": "basic", "secret": "docs-token"},
    ],
)
def test_remote_connector_only_takes_header_style_auth(auth: dict[str, Any]) -> None:
    """remote 连接器的凭据只能放在请求头里：bearer 或 header；query 与 basic 不支持。"""
    with pytest.raises(ValidationError, match="bearer"):
        ConnectorSpec.model_validate(
            {"name": "d", "transport": "remote", "url": "https://x/mcp", "auth": auth}
        )


@pytest.mark.parametrize(
    "base",
    [
        {"name": "d", "command": ["uvx", "x"]},
        {"name": "d", "transport": "http", "openapi": "a.yaml"},
    ],
)
def test_url_only_belongs_to_remote_connectors(base: dict[str, Any]) -> None:
    """url 只用于 remote 连接器：stdio 与 http 连接器写了 url 都报错。"""
    with pytest.raises(ValidationError, match="url"):
        ConnectorSpec.model_validate({**base, "url": "https://x/mcp"})


def test_stdio_connector_still_rejects_auth() -> None:
    """stdio 连接器仍然不能写 auth（凭据经 env 的 secret 注入）。"""
    with pytest.raises(ValidationError, match="auth"):
        ConnectorSpec.model_validate(
            {"name": "d", "command": ["x"], "auth": {"type": "bearer", "secret": "t"}}
        )


def test_remote_connector_in_manifest_roundtrip(samples: Any) -> None:
    """清单里的 remote 连接器能解析、写回再解析；JSON Schema 里有 remote 与 url。"""
    remote = {
        "name": "docs",
        "transport": "remote",
        "url": "https://mcp.example.com/mcp",
        "auth": {"type": "bearer", "secret": "docs-token"},
        "tools": [{"name": "search", "effect": "read"}],
    }
    data = yaml.safe_load(samples.manifest)
    data["spec"]["connectors"].append(remote)
    manifest = parse_manifest(yaml.safe_dump(data, allow_unicode=True))
    assert manifest.spec.secret_names == ("crm-token", "docs-token")
    assert parse_manifest(dump_manifest(manifest)) == manifest
    connector = manifest_json_schema()["$defs"]["ConnectorSpec"]
    assert "remote" in connector["properties"]["transport"]["enum"]
    assert "url" in connector["properties"]


def test_version_ordering(samples: Any) -> None:
    """版本排序遵循语义化版本：预发布版在正式版之前，数字段按数值比。"""
    ordered = [
        "0.9.0",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.10.0",
        "2.0.0",
    ]
    assert sorted(reversed(ordered), key=version_key) == ordered
    with pytest.raises(ValueError, match="语义化版本"):
        version_key("1.0")
