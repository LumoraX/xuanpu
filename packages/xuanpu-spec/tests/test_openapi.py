"""OpenAPI 文档的解析，以及 HTTP 连接器的清单与包校验。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from xuanpu_spec import ManifestError, OpenApiError, parse_manifest, parse_openapi, validate_package

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DOCUMENT: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "CRM"},
    "servers": [{"url": "https://crm.internal/api"}],
    "paths": {
        "/customers/{id}": {
            "parameters": [{"name": "id", "in": "path", "schema": {"type": "string"}}],
            "get": {
                "operationId": "getCustomer",
                "summary": "按编号取客户",
                "parameters": [
                    {"name": "expand", "in": "query", "schema": {"type": "string"}},
                    {"name": "X-Trace", "in": "header", "description": "追踪号"},
                    {"name": "sid", "in": "cookie"},
                ],
            },
            "delete": {"summary": "删除客户"},
        },
        "/tickets": {
            "post": {
                "operationId": "createTicket",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Ticket"}}
                    },
                },
            }
        },
    },
    "components": {
        "schemas": {
            "Ticket": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "owner": {"$ref": "#/components/schemas/User"},
                },
                "required": ["title"],
            },
            "User": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    },
}


def dump(document: dict[str, Any]) -> str:
    """文档 → YAML 文本。"""
    return yaml.safe_dump(document, allow_unicode=True)


def edited(**changes: Any) -> str:
    """在合法文档的基础上改顶层键。"""
    document = yaml.safe_load(dump(DOCUMENT))
    document.update(changes)
    return dump(document)


def test_operations_and_parameters() -> None:
    """每个方法加路径是一个 operation；路径级参数并入；cookie 参数跳过；路径参数必填。"""
    document = parse_openapi(dump(DOCUMENT))
    assert document.title == "CRM"
    assert document.servers == ("https://crm.internal/api",)
    assert sorted(document.operations) == ["createTicket", "delete_customers_id", "getCustomer"]

    get = document.operations["getCustomer"]
    assert (get.method, get.path, get.summary, get.read_only) == (
        "get", "/customers/{id}", "按编号取客户", True,
    )  # fmt: skip
    assert [(p.name, p.location, p.required) for p in get.parameters] == [
        ("id", "path", True),
        ("expand", "query", False),
        ("X-Trace", "header", False),
    ]
    assert get.input_schema() == {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "expand": {"type": "string"},
            "X-Trace": {"type": "string", "description": "追踪号"},
        },
        "required": ["id"],
    }
    assert document.operations["delete_customers_id"].read_only is False


def test_request_body_refs_are_expanded() -> None:
    """请求体里的 $ref 被展开成完整的 schema，模型不需要看 components。"""
    create = parse_openapi(dump(DOCUMENT)).operations["createTicket"]
    schema = create.input_schema()
    assert schema["required"] == ["body"]
    assert schema["properties"]["body"] == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "owner": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
        "required": ["title"],
    }


def test_json_documents_are_accepted() -> None:
    """JSON 写的文档同样能解析。"""
    import json

    assert "getCustomer" in parse_openapi(json.dumps(DOCUMENT)).operations


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "顶层"),
        ("a: [", "YAML"),
        (edited(openapi="2.0"), "3.x"),
        (edited(paths={}), "paths"),
        (edited(paths={"/x": {"get": {"operationId": "bad id"}}}), "标识符"),
        (
            edited(paths={"/a": {"get": {"operationId": "dup"}}, "/b": {"get": {"operationId": "dup"}}}),
            "重复",
        ),
        (
            edited(paths={"/x": {"get": {"parameters": [{"name": "Authorization", "in": "header"}]}}}),
            "由平台设置",
        ),
        (
            edited(paths={"/x": {"get": {"parameters": [{"$ref": "other.yaml#/p"}]}}}),
            "外部引用",
        ),
        (
            edited(paths={"/x": {"get": {"parameters": [{"$ref": "#/components/nope"}]}}}),
            "引用不存在",
        ),
        (
            edited(
                paths={"/x/{id}": {"get": {"parameters": [
                    {"name": "id", "in": "path"}, {"name": "id", "in": "query"},
                ]}}}
            ),
            "重名",
        ),
        (
            edited(
                paths={"/x": {"post": {"requestBody": {"content": {"text/plain": {}}}}}}
            ),
            "application/json",
        ),
        (
            edited(
                paths={"/x": {"post": {
                    "parameters": [{"name": "body", "in": "query"}],
                    "requestBody": {"content": {"application/json": {}}},
                }}}
            ),
            "冲突",
        ),
    ],
)  # fmt: skip
def test_unsupported_documents_are_rejected(text: str, fragment: str) -> None:
    """不支持或有歧义的写法明确报错，不猜。"""
    with pytest.raises(OpenApiError, match=fragment):
        parse_openapi(text)


def test_cyclic_refs_do_not_hang() -> None:
    """引用成环时报错，不无限递归。"""
    document = yaml.safe_load(dump(DOCUMENT))
    document["components"]["schemas"]["User"] = {
        "type": "object",
        "properties": {"boss": {"$ref": "#/components/schemas/User"}},
    }
    with pytest.raises(OpenApiError, match="成环"):
        parse_openapi(dump(document))


HTTP_MANIFEST = """\
apiVersion: xuanpu/v1
metadata: {name: crm-desk, version: 1.0.0}
spec:
  skills: {entries: [triage]}
  connectors:
    - name: crm
      transport: http
      openapi: apis/crm.yaml
      auth: {type: bearer, secret: crm-token}
      tools:
        - {name: getCustomer}
        - {name: createTicket, effect: write}
  evals:
    - {name: greet, entry: triage, input: 你好}
"""


def test_http_connector_manifest() -> None:
    """HTTP 连接器：工具名前缀是 api__；GET 默认只读；密钥名进引用清单。"""
    connector = parse_manifest(HTTP_MANIFEST).spec.connectors[0]
    assert connector.tool_name("getCustomer") == "api__crm__getCustomer"
    assert connector.effect_of("getCustomer", read_only=True) == "read"
    assert connector.effect_of("createTicket", read_only=False) == "write"
    assert connector.effect_of("other", read_only=False) == "external"
    assert connector.secret_names == ("crm-token",)
    assert connector.timeout_seconds == 30.0


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ("      openapi: apis/crm.yaml\n", "", "必须给 openapi"),
        ("      transport: http\n", "", "必须给 command"),
        ("      openapi: apis/crm.yaml\n", "      openapi: apis/crm.yaml\n      command: [x]\n", "只用于 stdio"),
        ("{type: bearer, secret: crm-token}", "{type: header, secret: crm-token}", "必须给 header"),
        ("{type: bearer, secret: crm-token}", "{type: query, secret: crm-token}", "必须给 name"),
        (
            "{type: bearer, secret: crm-token}",
            "{type: header, secret: crm-token, header: Authorization}",
            "bearer",
        ),
        ("{type: bearer, secret: crm-token}", "{type: oauth, secret: crm-token}", "type"),
        ("      openapi: apis/crm.yaml\n", "      openapi: apis/crm.yaml\n      baseUrl: ftp://x\n", "http://"),
        ("      openapi: apis/crm.yaml\n", "      openapi: apis/crm.yaml\n      timeoutSeconds: 0\n", "timeoutSeconds"),
    ],
)  # fmt: skip
def test_invalid_http_connector(old: str, new: str, fragment: str) -> None:
    """两种传输的字段不能混用；凭据注入方式要写全。"""
    assert old in HTTP_MANIFEST
    with pytest.raises(ManifestError, match=fragment):
        parse_manifest(HTTP_MANIFEST.replace(old, new))


def _package(make_package: Callable[..., Path], samples: Any, *, tool: str, document: str) -> Path:
    """搭一个带 HTTP 连接器的业务包。"""
    triage = samples.triage.replace("mcp__crm__search_customer", tool)
    root = make_package(manifest=HTTP_MANIFEST, skills={"triage": triage, "faq": samples.faq})
    (root / "apis").mkdir()
    (root / "apis" / "crm.yaml").write_text(document, encoding="utf-8")
    return root


def test_package_with_http_connector_validates(
    make_package: Callable[..., Path], samples: Any
) -> None:
    """合法的 HTTP 连接器业务包通过校验。"""
    root = _package(make_package, samples, tool="api__crm__getCustomer", document=dump(DOCUMENT))
    report = validate_package(root)
    assert report.ok, report.errors


def test_package_http_connector_problems(make_package: Callable[..., Path], samples: Any) -> None:
    """文档读不出、白名单对不上、前缀用错、没有基地址，逐条报出。"""
    no_servers = yaml.safe_load(dump(DOCUMENT))
    del no_servers["servers"]
    del no_servers["paths"]["/tickets"]
    root = _package(make_package, samples, tool="mcp__crm__getCustomer", document=dump(no_servers))
    report = validate_package(root)
    joined = "\n".join(report.errors)
    assert "前缀与连接器" in joined
    assert "'createTicket'，但 OpenAPI 文档里没有这个 operation" in joined
    assert "没有 baseUrl" in joined

    broken = _package(
        lambda **kw: make_package(name="pkg2", **kw), samples, tool="api__crm__getCustomer",
        document="openapi: 2.0\n",
    )  # fmt: skip
    assert any("OpenAPI 文档" in error for error in validate_package(broken).errors)

    missing = make_package(
        name="pkg3",
        manifest=HTTP_MANIFEST,
        skills={
            "triage": samples.triage.replace("mcp__crm__search_customer", "api__crm__getCustomer"),
            "faq": samples.faq,
        },
    )
    assert any("读不到 OpenAPI 文档" in error for error in validate_package(missing).errors)
