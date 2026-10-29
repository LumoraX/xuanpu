"""实际 F0 HTTP 输出必须符合发布包的严格响应契约。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import jsonschema
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_f0_e2e import SignedClient, app_for
from .test_parser import SAMPLE
from .test_storage import prepare
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from xuanpu_finance.storage import FinanceStore


def convert(value: Any) -> Any:
    """把 OpenAPI 3 nullable 转为 JSON Schema 的联合类型。"""
    if isinstance(value, list):
        return [convert(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: convert(item) for key, item in value.items() if key != "nullable"}
    return {"anyOf": [result, {"type": "null"}]} if value.get("nullable") else result


def published_contract() -> Any:
    """读取唯一发布契约并转换 nullable 语义。"""
    return convert(
        yaml.safe_load(
            (Path(__file__).resolve().parents[3] / "examples/finance/apis/finance.yaml").read_text()
        )
    )


class ResponseContract:
    """对实际 HTTP 返回按发布契约逐份验证。"""

    def __init__(self, contract: Any, checked: set[str]) -> None:
        """注入完整组件及已覆盖操作集合。"""
        self.contract, self.checked = contract, checked

    async def __call__(
        self, signed: SignedClient, method: str, route: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """按实际状态码取OpenAPI响应schema验证。"""
        response = await signed.call(method, path, **kwargs)
        operation = self.contract["paths"][route][method.lower()]
        self.checked.add(operation["operationId"])
        assert response.status_code < 300, response.text
        schema = operation["responses"][str(response.status_code)]
        if "$ref" in schema:
            schema = self.contract["components"]["responses"][schema["$ref"].split("/")[-1]]
        schema = {
            **schema["content"]["application/json"]["schema"],
            "components": self.contract["components"],
        }
        jsonschema.Draft4Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
            response.json()
        )
        return response.json()


async def test_actual_http_responses_match_published_contract(
    store: FinanceStore, tmp_path: Path
) -> None:
    """执行11个操作并验证每份真实JSON，不用静态样例冒充接口契约。"""
    contract = published_contract()
    await prepare(store)
    gateway = Gateway({"xml": SAMPLE, "pdf": b"%PDF-1.4"})
    key = Ed25519PrivateKey.generate()
    app = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), gateway, key)
    checked: set[str] = set()

    check = ResponseContract(contract, checked)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://finance"
    ) as c:
        signed = SignedClient(c, key)
        await check(signed, "GET", "/v1/finance/legal-entities", "/v1/finance/legal-entities")
        for name in ("integrations", "exceptions"):
            await check(
                signed,
                "GET",
                f"/v1/finance/{name}",
                f"/v1/finance/{name}",
                params={"legal_entity_id": "company-a"},
            )
        op = await check(
            signed,
            "POST",
            "/v1/finance/invoice-imports",
            "/v1/finance/invoice-imports",
            body={
                "legal_entity_id": "company-a",
                "files": [ref("xml", SAMPLE).model_dump(), ref("pdf", b"%PDF-1.4").model_dump()],
            },
            idem="contract",
        )
        await app.state.worker.run_once()
        await check(
            signed, "GET", "/v1/finance/operations/{id}", f"/v1/finance/operations/{op['id']}"
        )
        invoices = await check(
            signed,
            "GET",
            "/v1/finance/invoices",
            "/v1/finance/invoices",
            params={"legal_entity_id": "company-a"},
        )
        invoice_id = invoices["items"][0]["id"]
        for suffix in ("", "/lines", "/fields", "/evidence"):
            await check(
                signed,
                "GET",
                "/v1/finance/invoices/{id}" + suffix,
                f"/v1/finance/invoices/{invoice_id}" + suffix,
            )
        await check(
            signed,
            "GET",
            "/v1/finance/exceptions",
            "/v1/finance/exceptions",
            params={"legal_entity_id": "company-a"},
        )
        pending = await check(
            signed,
            "POST",
            "/v1/finance/invoice-imports",
            "/v1/finance/invoice-imports",
            body={"legal_entity_id": "company-a", "files": [ref("xml", SAMPLE).model_dump()]},
            idem="cancel-contract",
        )
        await check(
            signed,
            "POST",
            "/v1/finance/operations/{id}/cancel",
            f"/v1/finance/operations/{pending['id']}/cancel",
            body={"expected_version": pending["version"]},
            idem="cancel",
        )
    assert len(checked) == 11
