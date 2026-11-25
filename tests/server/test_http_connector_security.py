"""Security regression tests for HTTP connector path-parameter traversal and origin pinning."""

from __future__ import annotations

import pytest

from server.engine.runtime.http_api import build_request
from spec.openapi import Operation, Parameter


def _operation(path: str = "/v1/projects/public/{id}") -> Operation:
    return Operation(
        operation_id="getProject",
        method="delete",
        path=path,
        summary="Delete public project",
        parameters=(
            Parameter(
                name="id",
                location="path",
                required=True,
                schema={"type": "string"},
            ),
        ),
        request_body=None,
        request_body_required=False,
    )


@pytest.mark.parametrize("bad_segment", ["..", ".", ""])
def test_build_request_rejects_dot_and_empty_path_parameters(bad_segment: str) -> None:
    """Dot segments bypass urllib.parse.quote and normalize via httpx.URL to escape endpoint paths."""
    with pytest.raises(ValueError, match="路径参数"):
        build_request("https://api.internal", _operation(), {"id": bad_segment})


def test_build_request_allows_safe_dotted_identifiers_and_encodes_slashes() -> None:
    """Ordinary identifiers with dots still work and slashes remain percent-encoded."""
    method, url, _query, _headers, _body = build_request(
        "https://api.internal", _operation(), {"id": "v1.2.3"}
    )
    assert method == "DELETE"
    assert url == "https://api.internal/v1/projects/public/v1.2.3"

    _, slashed_url, _, _, _ = build_request("https://api.internal", _operation(), {"id": "a/b"})
    assert slashed_url == "https://api.internal/v1/projects/public/a%2Fb"


def test_build_request_enforces_base_url_origin_pinning() -> None:
    """Even a programmatically constructed Operation cannot redirect base_url + path to another host."""
    crafted = Operation(
        operation_id="steal",
        method="get",
        path="@evil.example/leak",
        summary="",
        parameters=(),
        request_body=None,
        request_body_required=False,
    )
    with pytest.raises(ValueError, match="baseUrl"):
        build_request("https://api.internal", crafted, {})
