"""跨服务请求摘要的规范与保留参数边界。"""

from __future__ import annotations

import json

import pytest
from xuanpu_spec import OpenApiError, parse_openapi


def test_fingerprint_canonical_and_sensitive() -> None:
    """摘要规范化顺序，并绑定幂等键与内容类型。"""
    from xuanpu_spec.call_context import request_fingerprint

    first = request_fingerprint("post", "/v1/a", [("z", "2"), ("a", "1")], {"b": 2, "a": 1})
    assert first == request_fingerprint("POST", "/v1/a", [("a", "1"), ("z", "2")], {"a": 1, "b": 2})
    assert first != request_fingerprint(
        "POST", "/v1/a", [("a", "1"), ("z", "2")], {"a": 1, "b": 2}, "key"
    )
    with pytest.raises(ValueError):
        request_fingerprint("POST", "/", [], {"x": float("nan")})


def test_context_header_is_reserved() -> None:
    """OpenAPI 不能显式开放平台签名头。"""
    with pytest.raises(OpenApiError, match="平台"):
        parse_openapi(
            json.dumps(
                {
                    "openapi": "3.0.3",
                    "info": {"title": "a"},
                    "paths": {
                        "/": {
                            "post": {
                                "operationId": "a",
                                "parameters": [{"name": "X-Xuanpu-Context", "in": "header"}],
                            }
                        }
                    },
                }
            )
        )


@pytest.mark.parametrize("location", ["query", "path", "cookie"])
def test_context_parameter_cannot_be_declared_anywhere(location: str) -> None:
    """保留名称不得通过参数位置变化重新暴露给模型。"""
    with pytest.raises(OpenApiError, match="平台"):
        parse_openapi(
            json.dumps(
                {
                    "openapi": "3.0.3",
                    "info": {"title": "a"},
                    "paths": {
                        "/": {
                            "post": {
                                "operationId": "a",
                                "parameters": [{"name": "x-xuanpu-context", "in": location}],
                            }
                        }
                    },
                }
            )
        )


@pytest.mark.parametrize('shared', [False, True])
def test_header_parameters_reject_case_insensitive_duplicates(shared: bool) -> None:
    """HTTP 不区分头大小写，路径级与 operation 级也不能开放两个别名。"""
    first = {'name': 'Idempotency-Key', 'in': 'header'}
    second = {'name': 'idempotency-key', 'in': 'header'}
    operation = {'operationId': 'a', 'parameters': [second] if shared else [first, second]}
    path = {'post': operation}
    if shared:
        path['parameters'] = [first]
    with pytest.raises(OpenApiError, match='重复'):
        parse_openapi(json.dumps({'openapi': '3.0.3', 'info': {'title': 'a'}, 'paths': {'/': path}}))


def test_header_parameters_reject_exact_duplicate_in_same_scope() -> None:
    """同一参数列表中的完全重复头也须报错，不能在合并字典时悄悄丢弃。"""
    header = {'name': 'Idempotency-Key', 'in': 'header'}
    with pytest.raises(OpenApiError, match='重复'):
        parse_openapi(json.dumps({'openapi': '3.0.3', 'info': {'title': 'a'}, 'paths': {'/': {
            'post': {'operationId': 'a', 'parameters': [header, header]}}}}))


def test_query_canonicalization_preserves_repeated_value_order() -> None:
    """同名查询参数的先后可能改变业务取值；只规整不同键，不得重排同名值。"""
    from xuanpu_spec.call_context import request_fingerprint

    forward = [('legal_entity_id', 'a'), ('legal_entity_id', 'b'), ('page', '1')]
    reversed_values = [('legal_entity_id', 'b'), ('legal_entity_id', 'a'), ('page', '1')]
    reordered_keys = [('page', '1'), ('legal_entity_id', 'a'), ('legal_entity_id', 'b')]
    assert request_fingerprint('GET', '/', forward, None) != request_fingerprint('GET', '/', reversed_values, None)
    assert request_fingerprint('GET', '/', forward, None) == request_fingerprint('GET', '/', reordered_keys, None)
