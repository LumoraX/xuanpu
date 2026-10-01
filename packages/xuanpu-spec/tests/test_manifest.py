"""清单的解析与字段约束。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from xuanpu_spec import (
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
