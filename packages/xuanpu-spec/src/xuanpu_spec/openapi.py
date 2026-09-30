"""读取 OpenAPI 文档，取出把每个 operation 变成工具所需的信息。

只做这一件事，不是完整的 OpenAPI 实现：支持 OpenAPI 3.x 的 ``paths``、``parameters``
（path / query / header）、``requestBody`` 的 JSON schema，以及 ``components`` 内的 ``$ref``
引用。不支持的部分（``callbacks``、``links``、外部 ``$ref``）忽略或报错，不猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import yaml

from xuanpu_spec.errors import SpecError

if TYPE_CHECKING:
    from pathlib import Path

METHODS: tuple[str, ...] = ("get", "put", "post", "delete", "patch", "head", "options")
ParameterLocation = Literal["path", "query", "header"]

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_REF_DEPTH = 32
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SLUG = re.compile(r"[^A-Za-z0-9]+")
# 请求方自己会放的头，不允许 OpenAPI 参数覆盖
_RESERVED_HEADERS = frozenset({"authorization", "host", "content-length", "content-type"})


class OpenApiError(SpecError):
    """OpenAPI 文档缺失、无法解析或不受支持。"""


@dataclass(frozen=True)
class Parameter:
    """一个参数。"""

    name: str
    location: ParameterLocation
    required: bool
    schema: dict[str, Any]
    description: str = ""


@dataclass(frozen=True)
class Operation:
    """一个 operation：一个方法加一条路径。"""

    operation_id: str
    method: str
    path: str
    summary: str
    parameters: tuple[Parameter, ...] = ()
    request_body: dict[str, Any] | None = None
    request_body_required: bool = False

    @property
    def read_only(self) -> bool:
        """按 HTTP 语义是否只读。"""
        return self.method in ("get", "head")

    def input_schema(self) -> dict[str, Any]:
        """模型看到的参数 schema：路径、查询、头参数与请求体平铺成一个对象。

        请求体放在 ``body`` 键下；参数名与 ``body`` 冲突的文档在解析时已被拒绝。
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in self.parameters:
            described = dict(parameter.schema)
            if parameter.description and "description" not in described:
                described["description"] = parameter.description
            properties[parameter.name] = described
            if parameter.required:
                required.append(parameter.name)
        if self.request_body is not None:
            properties["body"] = self.request_body
            if self.request_body_required:
                required.append("body")
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema


@dataclass(frozen=True)
class ApiDocument:
    """一份解析后的 OpenAPI 文档。"""

    title: str
    servers: tuple[str, ...]
    operations: dict[str, Operation] = field(default_factory=dict)


class _Resolver:
    """展开 ``components`` 内的 ``$ref``。"""

    def __init__(self, document: dict[str, Any]) -> None:
        """记住整份文档以便查引用。"""
        self._document = document

    def lookup(self, ref: str) -> Any:
        """按 ``#/a/b/c`` 找到目标。"""
        if not ref.startswith("#/"):
            raise OpenApiError(f"不支持外部引用：{ref}")
        node: Any = self._document
        for part in ref[2:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or key not in node:
                raise OpenApiError(f"引用不存在：{ref}")
            node = node[key]
        return node

    def expand(self, value: Any, depth: int = 0) -> Any:
        """递归展开引用；引用成环或过深时报错，不无限递归。"""
        if depth > _MAX_REF_DEPTH:
            raise OpenApiError("$ref 嵌套过深或成环")
        if isinstance(value, dict):
            if "$ref" in value and isinstance(value["$ref"], str):
                return self.expand(self.lookup(value["$ref"]), depth + 1)
            return {key: self.expand(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [self.expand(item, depth + 1) for item in value]
        return value


def _slug(method: str, path: str) -> str:
    """没有 operationId 时按方法与路径生成一个。"""
    return _SLUG.sub("_", f"{method}_{path}").strip("_")


def _parameter(raw: Any, where: str) -> Parameter | None:
    """解析一个参数；不支持的位置（cookie）跳过。"""
    if not isinstance(raw, dict) or "name" not in raw or "in" not in raw:
        raise OpenApiError(f"{where}：参数缺少 name 或 in")
    location = str(raw["in"])
    if location == "cookie":
        return None
    if location not in ("path", "query", "header"):
        raise OpenApiError(f"{where}：不支持的参数位置 {location!r}")
    name = str(raw["name"])
    if location == "header" and name.lower() in _RESERVED_HEADERS:
        raise OpenApiError(f"{where}：头参数 {name!r} 由平台设置，不能作为工具参数")
    declared = raw.get("schema")
    schema: dict[str, Any] = dict(declared) if isinstance(declared, dict) else {"type": "string"}
    return Parameter(
        name=name,
        location=location,  # type: ignore[arg-type]
        required=bool(raw.get("required")) or location == "path",
        schema=schema,
        description=str(raw.get("description") or ""),
    )


def _request_body(raw: Any, where: str) -> tuple[dict[str, Any] | None, bool]:
    """取 JSON 请求体的 schema。只认 ``application/json``。"""
    if not isinstance(raw, dict):
        return None, False
    content = raw.get("content")
    if not isinstance(content, dict):
        return None, False
    media = content.get("application/json")
    if not isinstance(media, dict):
        raise OpenApiError(f"{where}：请求体只支持 application/json")
    schema = media.get("schema")
    return (dict(schema) if isinstance(schema, dict) else {"type": "object"}), bool(
        raw.get("required")
    )


def _operation(method: str, path: str, raw: dict[str, Any], shared: list[Parameter]) -> Operation:
    """解析一个 operation。路径级参数与 operation 级参数合并，后者覆盖前者。"""
    where = f"{method.upper()} {path}"
    operation_id = str(raw.get("operationId") or _slug(method, path))
    if not _IDENT.match(operation_id):
        raise OpenApiError(f"{where}：operationId {operation_id!r} 不是合法的标识符")
    merged: dict[tuple[str, str], Parameter] = {(p.location, p.name): p for p in shared}
    for item in raw.get("parameters") or []:
        parameter = _parameter(item, where)
        if parameter is not None:
            merged[(parameter.location, parameter.name)] = parameter
    parameters = tuple(merged.values())
    names = [parameter.name for parameter in parameters]
    if len(set(names)) != len(names):
        raise OpenApiError(f"{where}：不同位置的参数重名，无法平铺成工具参数")
    body, body_required = _request_body(raw.get("requestBody"), where)
    if body is not None and "body" in names:
        raise OpenApiError(f"{where}：参数名 body 与请求体冲突")
    return Operation(
        operation_id=operation_id,
        method=method,
        path=path,
        summary=str(raw.get("summary") or raw.get("description") or ""),
        parameters=parameters,
        request_body=body,
        request_body_required=body_required,
    )


def parse_openapi(text: str) -> ApiDocument:
    """从 YAML 或 JSON 文本解析。

    Raises:
        OpenApiError: 不是 OpenAPI 3.x、无法解析或含不支持的写法。
    """
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise OpenApiError(f"OpenAPI 文档超过 {MAX_DOCUMENT_BYTES} 字节")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OpenApiError(f"OpenAPI 文档不是合法的 YAML / JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise OpenApiError("OpenAPI 文档的顶层必须是映射")
    version = str(raw.get("openapi") or "")
    if not version.startswith("3."):
        raise OpenApiError(f"只支持 OpenAPI 3.x，得到 {version or '未声明'!r}")
    resolver = _Resolver(raw)
    paths = raw.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise OpenApiError("OpenAPI 文档没有 paths")
    operations: dict[str, Operation] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            raise OpenApiError(f"路径 {path} 的定义不是映射")
        expanded = resolver.expand(item)
        shared = [
            parameter
            for entry in expanded.get("parameters") or []
            if (parameter := _parameter(entry, str(path))) is not None
        ]
        for method in METHODS:
            if method not in expanded:
                continue
            operation = _operation(method, str(path), expanded[method], shared)
            if operation.operation_id in operations:
                raise OpenApiError(f"operationId 重复：{operation.operation_id}")
            operations[operation.operation_id] = operation
    declared_info = raw.get("info")
    info: dict[str, Any] = declared_info if isinstance(declared_info, dict) else {}
    servers = tuple(
        str(server["url"])
        for server in (raw.get("servers") or [])
        if isinstance(server, dict) and isinstance(server.get("url"), str)
    )
    return ApiDocument(title=str(info.get("title") or ""), servers=servers, operations=operations)


def load_openapi(path: Path) -> ApiDocument:
    """从文件读取。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OpenApiError(f"读不到 OpenAPI 文档 {path.name}：{exc}") from exc
    return parse_openapi(text)


__all__ = [
    "METHODS",
    "ApiDocument",
    "OpenApiError",
    "Operation",
    "Parameter",
    "load_openapi",
    "parse_openapi",
]
