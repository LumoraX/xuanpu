"""Security regression tests for OpenAPI parser expansion bounds and path validation."""

from __future__ import annotations

import pytest
import yaml

from spec import OpenApiError, parse_openapi


def test_rejects_exponential_ref_expansion_bomb() -> None:
    """A shallow $ref fan-out DAG (depth < 32, 6^8 nodes) must fail fast with OpenApiError."""
    schemas: dict[str, object] = {"L0": {"type": "string"}}
    for level in range(1, 9):
        prev = f"#/components/schemas/L{level - 1}"
        schemas[f"L{level}"] = {
            "type": "object",
            "properties": {f"p{i}": {"$ref": prev} for i in range(6)},
        }
    doc = {
        "openapi": "3.0.3",
        "paths": {
            "/boom": {
                "post": {
                    "operationId": "boom",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/L8"}
                            }
                        }
                    },
                }
            }
        },
        "components": {"schemas": schemas},
    }
    with pytest.raises(OpenApiError, match="展开规模"):
        parse_openapi(yaml.safe_dump(doc))


def test_rejects_yaml_alias_expansion_bomb() -> None:
    """PyYAML anchors/aliases that form a compact exponential DAG must fail fast."""
    bomb = """
openapi: "3.0.3"
x-defs:
  a0: &a0 {type: string}
  a1: &a1 {p1: *a0, p2: *a0, p3: *a0, p4: *a0}
  a2: &a2 {p1: *a1, p2: *a1, p3: *a1, p4: *a1}
  a3: &a3 {p1: *a2, p2: *a2, p3: *a2, p4: *a2}
  a4: &a4 {p1: *a3, p2: *a3, p3: *a3, p4: *a3}
  a5: &a5 {p1: *a4, p2: *a4, p3: *a4, p4: *a4}
  a6: &a6 {p1: *a5, p2: *a5, p3: *a5, p4: *a5}
  a7: &a7 {p1: *a6, p2: *a6, p3: *a6, p4: *a6}
  a8: &a8 {p1: *a7, p2: *a7, p3: *a7, p4: *a7}
  a9: &a9 {p1: *a8, p2: *a8, p3: *a8, p4: *a8}
paths:
  /boom:
    post:
      operationId: boom
      requestBody:
        content:
          application/json:
            schema: *a9
"""
    with pytest.raises(OpenApiError, match="展开规模"):
        parse_openapi(bomb)


@pytest.mark.parametrize(
    "bad_path",
    [
        "@evil.com/steal",
        ".evil.com/steal",
        "relative/path",
        "/items?injected=1",
        "/items#frag",
        "/items with space",
    ],
)
def test_rejects_malformed_or_host_hijacking_paths(bad_path: str) -> None:
    """OpenAPI paths must start with '/' and exclude query, fragment, and whitespace."""
    doc = {
        "openapi": "3.0.3",
        "paths": {bad_path: {"get": {"operationId": "op"}}},
    }
    with pytest.raises(OpenApiError, match="必须以 / 开头"):
        parse_openapi(yaml.safe_dump(doc))


def test_rejects_non_dict_operation_and_non_list_parameters_cleanly() -> None:
    """Malformed operation or parameters shapes raise OpenApiError instead of AttributeError/TypeError."""
    null_op = {"openapi": "3.0.3", "paths": {"/items": {"get": None}}}
    with pytest.raises(OpenApiError, match="必须是映射"):
        parse_openapi(yaml.safe_dump(null_op))

    bad_path_params = {
        "openapi": "3.0.3",
        "paths": {"/items": {"parameters": "not-a-list", "get": {"operationId": "listItems"}}},
    }
    with pytest.raises(OpenApiError, match="parameters 必须是列表"):
        parse_openapi(yaml.safe_dump(bad_path_params))

    bad_op_params = {
        "openapi": "3.0.3",
        "paths": {"/items": {"get": {"operationId": "listItems", "parameters": {"name": "x"}}}},
    }
    with pytest.raises(OpenApiError, match="parameters 必须是列表"):
        parse_openapi(yaml.safe_dump(bad_op_params))
