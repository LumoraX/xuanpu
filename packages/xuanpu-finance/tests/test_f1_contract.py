"""F1实际签名HTTP响应对照发布包，防止文档与执行脱节。"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_contract import ResponseContract, convert, published_contract
from .test_expense_domain import invoice, setup
from .test_f0_e2e import SignedClient, app_for
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from xuanpu_finance.storage import FinanceStore


async def test_all_f1_actual_http_responses_match_package(store: FinanceStore, tmp_path: Path) -> None:
    """执行新增14操作及当前工作流响应，验证严格契约而非手写样例。"""
    owner, _ = await setup(store)
    await invoice(store)
    key = Ed25519PrivateKey.generate()
    app = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({"pdf": b"%PDF-1.4"}), key)
    contract = published_contract()
    catalog = convert(yaml.safe_load((Path(__file__).resolve().parents[3] / "examples/finance/apis/catalog.yaml").read_text()))
    checked: set[str] = set()
    check, cat = ResponseContract(contract, checked), ResponseContract(catalog, checked)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://finance") as client:
        signed = SignedClient(client, key)
        async def call(method: str, route: str, path: str | None = None, *, user: str = owner.user_id, catalog: bool = False, **kwargs: Any) -> dict[str, Any]:
            """保留实际验签与独立PG，目录使用自己的组件表。"""
            return await (cat if catalog else check)(signed, method, route, path or route, tenant="t", origin="human", user=user, **kwargs)
        await call("GET", "/v1/finance/catalog/expense-categories", params={"legal_entity_id": "e"}, catalog=True)
        await call("GET", "/v1/finance/catalog/cost-centers", params={"legal_entity_id": "e"}, catalog=True)
        draft = {"legal_entity_id":"e", "reason":"交通费", "lines":[{"category_id":"travel","amount":"600.00","occurred_on":"2026-10-21","reason":"出差"}]}
        doc = await call("POST", "/v1/finance/expense-claims", body=draft, idem="claim")
        await call("GET", "/v1/finance/expense-claims", params={"legal_entity_id":"e"})
        path = "/v1/finance/expense-claims/" + doc["id"]
        doc = await call("PUT", "/v1/finance/expense-claims/{id}", path, body={"expected_version":doc["version"],"draft":draft}, idem="revise")
        allocation_body = {"invoice_id":"i","invoice_version":1,"document_id":doc["id"],"document_kind":"expense_claim","expected_document_version":doc["version"],"amount":"600.00","tax":"0.00"}
        alloc = await call("POST", "/v1/finance/invoice-allocations", body=allocation_body, idem="alloc")
        await call("GET", "/v1/finance/invoice-allocations/{id}", "/v1/finance/invoice-allocations/"+alloc["id"], catalog=True)
        await call("POST", "/v1/finance/invoice-allocations/{id}/release", "/v1/finance/invoice-allocations/"+alloc["id"]+"/release", body={"expected_version":alloc["version"]}, idem="release")
        doc = await call("GET", "/v1/finance/expense-claims/{id}", path)
        allocation_body["expected_document_version"] = doc["version"]
        await call("POST", "/v1/finance/invoice-allocations", body=allocation_body, idem="alloc2")
        doc = await call("GET", "/v1/finance/expense-claims/{id}", path)
        async def action(user: str, action: str, params: dict[str, Any]) -> None:
            """预检与人工执行分别验证公开响应。"""
            plan = await call("POST", "/v1/finance/action-previews", user=user, body={"action":action,"target_type":"expense_claim","target_id":doc["id"],"expected_version":doc["version"],"parameters":params}, idem="plan-"+action)
            await call("GET", "/v1/finance/action-plans/{id}", "/v1/finance/action-plans/"+plan["id"], user=user)
            assert plan["executable"], plan["blockers"]
            await call("POST", "/v1/finance/action-executions", user=user, body={"plan_id":plan["id"],"plan_digest":plan["digest"]}, idem="execute-"+action)
        await action(owner.user_id,"submit_claim",{})
        doc = await call("GET", "/v1/finance/expense-claims/{id}", path)
        queue = await call("GET", "/v1/finance/review-queue", user="a", params={"legal_entity_id":"e"})
        await action("a","approve",{"workflow_step_id":queue["items"][0]["workflow_step_id"]})
        approved = await call("GET", "/v1/finance/expense-claims/{id}", path)
        assert approved["status"] == "approved"
        accepted = await signed.call("POST", "/v1/finance/invoice-imports", tenant="t", user=owner.user_id, body={"legal_entity_id":"e","files":[ref("pdf",b"%PDF-1.4").model_dump()]}, idem="pdf")
        assert accepted.status_code == 202, accepted.text
        await app.state.worker.run_once()
        cases = await signed.call("GET", "/v1/finance/exceptions", tenant="t", user=owner.user_id, params={"legal_entity_id":"e"})
        case = cases.json()["items"][0]
        updated = await call("POST", "/v1/finance/exceptions/{id}/resolutions", "/v1/finance/exceptions/"+case["id"]+"/resolutions", body={"expected_version":case["version"],"disposition":"attach_evidence","reason":"附加实际收件材料，仍待核实","evidence_ids":[case["evidence"][0]["id"]]}, idem="attach")
        assert updated["version"] == case["version"] + 1
        assert updated["status"] == "investigating"
    assert len(checked) == 14
