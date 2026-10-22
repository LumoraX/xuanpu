"""已发布F0—F3业务包的工具、Skill、实际路由及请求契约。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import taifeng
import yaml
from fastapi.routing import APIRoute
from xuanpu_finance.expenses.models import ActionPreview
from xuanpu_finance.routes import router as f0_router
from xuanpu_finance.routes_f1 import router as f1_router
from xuanpu_finance.routes_f2 import router as f2_router
from xuanpu_finance.routes_f3 import router as f3_router
from xuanpu_finance.routes_f4 import router as f4_router
from xuanpu_finance.routes_f5 import router as f5_router
from xuanpu_finance.routes_f6 import router as f6_router
from xuanpu_finance.routes_f7 import router as f7_router
from xuanpu_spec import validate_package

PACKAGE = Path(__file__).resolve().parents[3] / "examples/finance"
SKILLS = {
    "finance-assistant",
    "invoice-intake",
    "expense-reimbursement",
    "finance-review",
    "accounts-payable",
    "posting-reconciliation",
    "archive-audit",
}


def contracts() -> dict[str, Any]:
    """加载可运行包的两个进程外HTTP契约。"""
    return {
        path.stem: yaml.safe_load(path.read_text()) for path in (PACKAGE / "apis").glob("*.yaml")
    }


def operations(document: Any) -> dict[str, tuple[str, str, Any]]:
    """以真实operationId索引路径和方法，不计公共path参数。"""
    return {
        operation["operationId"]: (path, method, operation)
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
        if isinstance(operation, dict) and "operationId" in operation
    }


def validate_schema(name: str, value: Any) -> None:
    """对发布schema验证真实或受控命令，保留完整本地引用。"""
    from .test_contract import convert

    document = convert(contracts()["finance"])
    jsonschema.Draft4Validator(
        {"$ref": "#/components/schemas/" + name, "components": document["components"]}
    ).validate(value)


async def test_f0_f2_package_and_kernel_reference_read() -> None:
    """稳定内核读取七个真实Skill，附属材料仍能实际加载。"""
    report = validate_package(PACKAGE)
    assert report.ok, report.errors
    registry = await taifeng.FilesystemSkillRegistry.load(PACKAGE / "skills")
    snapshot = registry.snapshot()
    assert {skill.id for skill in snapshot.skills} == SKILLS
    tool = taifeng.make_read_skill_tool()
    context = taifeng.ToolContext(
        call_id="test",
        cancel=taifeng.CancellationToken(),
        thread_id="test",
        extras={"skill_snapshot": snapshot, "visible_skills": frozenset(SKILLS)},
    )
    for skill_id in ("invoice-intake", "accounts-payable", "posting-reconciliation"):
        result = await tool.handler(
            {"skill_id": skill_id, "path": "references/execution-rules.md"}, context
        )
        assert not result.is_error
        assert "not_checked" in result.output or "outcome_unknown" in result.output


def test_runnable_package_has_exactly_the_72_actual_finance_operations() -> None:
    """包只能发布已交付F3工具，也不能遗漏人工入口依赖的已实现路由。"""
    actual = {
        route.operation_id: (route.path, next(iter(route.methods)).lower())
        for router in (f0_router, f1_router, f2_router, f3_router, f4_router, f5_router, f6_router, f7_router)
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    declared = {
        key: (path, method)
        for document in contracts().values()
        for key, (path, method, _) in operations(document).items()
    }
    assert len(declared) == 72 and declared == actual
    manifest = yaml.safe_load((PACKAGE / "xuanpu.yaml").read_text())
    connectors = manifest["spec"]["connectors"]
    assert {tool["name"] for connector in connectors for tool in connector["tools"]} == set(actual)
    assert not any("callback" in path for path, _ in declared.values())
    for document in contracts().values():
        assert len(document["paths"]) > 0
        assert all(
            operation.get("x-delivery-phase") in {"F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7"}
            for _, _, operation in operations(document).values()
        )


def test_model_skills_never_publish_human_execution_tools() -> None:
    """实际业务决定留给cookie人工网关，不因tool确认升级模型身份。"""
    for path in (PACKAGE / "skills").glob("*/SKILL.md"):
        frontmatter = yaml.safe_load(path.read_text().split("---", 2)[1])
        assert "api__finance__getFundingMaterial" not in frontmatter.get("tool_names", [])
        assert "api__finance__revokeFundingAuthority" not in frontmatter.get("tool_names", [])
        assert "api__finance__executeFinanceAction" not in frontmatter.get("tool_names", [])
        assert "api__finance__readFinanceNotification" not in frontmatter.get("tool_names", [])
        assert "api__finance__reconcileOperation" not in frontmatter.get("tool_names", [])
        assert "api__finance__linkRedLetterInvoice" not in frontmatter.get("tool_names", [])
        assert "api__finance__recordTaxAdjustmentEvidence" not in frontmatter.get("tool_names", [])
    assert "executeFinanceAction" in operations(contracts()["finance"])


@pytest.mark.parametrize(
    "action,target,parameters",
    [
        ("submit_claim", "expense_claim", {}),
        ("submit_payable", "payable", {}),
        ("approve", "action_plan", {"workflow_step_id": "step"}),
        ("reject", "payable", {"workflow_step_id": "step", "reason": "缺材料"}),
        ("withdraw", "payable", {"reason": "撤回"}),
        (
            "execute_payment",
            "payable",
            {
                "amount": "10.00",
                "payee_ref": {
                    "kind": "supplier_account",
                    "source": "controlled",
                    "id": "a",
                    "version": "v1",
                    "sha256": "a" * 64,
                },
            },
        ),
        ("submit_posting", "expense_claim", {"amount": "10.00", "accounting_period": "2026-10"}),
        (
            "reverse_posting",
            "posting",
            {"amount": "10.00", "accounting_period": "2026-10", "reason": "冲回"},
        ),
    ],
)
def test_published_action_matrix_matches_actual_strict_command(
    action: str, target: str, parameters: dict[str, Any]
) -> None:
    """每个已交付动作接受实际参数，并拒绝额外身份、结果和不适用字段。"""
    body = {
        "action": action,
        "target_type": target,
        "target_id": "target",
        "expected_version": 1,
        "parameters": parameters,
    }
    ActionPreview.model_validate(body)
    validate_schema("ActionPreviewRequest", body)
    bad = deepcopy(body)
    bad["parameters"]["confirmation_id"] = "forged"
    with pytest.raises(jsonschema.ValidationError):
        validate_schema("ActionPreviewRequest", bad)


async def test_f2_actual_persisted_results_match_published_contract(store: Any) -> None:
    """真实独立PG产生计划、资金和凭证记录，再验证发布响应schema。"""
    import json

    from .test_expense_domain import execute
    from .test_f2_domain import action_plan, approved_payable, provider_result

    owner, payer, accountant, approver, document = await approved_payable(store)
    validate_schema("BusinessDocument", document)
    validate_schema("PayablePage", await store.list_payables(owner, "e"))
    validate_schema("SettlementOptions", await store.get_settlement_options(payer, "e"))
    for action, actor in (("execute_payment", payer), ("submit_posting", accountant)):
        plan = await action_plan(store, actor, approver, document, action)
        validate_schema("ActionPlan", plan)
        validate_schema("ActionPlanPage", await store.list_action_plans(actor, "e"))
        operation = await execute(store, actor, plan)
        validate_schema("Operation", operation)
        if action == "execute_payment":
            validate_schema("PaymentPage", await store.list_payments(actor, "e"))
        else:
            validate_schema("PostingPage", await store.list_postings(actor, "e"))
        job = await store.claim_settlement()
        request = await store.prepare_settlement_dispatch(job)
        result = provider_result(request)
        await store.record_settlement_result(job, result, json.dumps(result).encode())
        validate_schema("Operation", await store.get_operation(actor, operation["id"]))
        if action == "execute_payment":
            records = await store.list_payments(actor, "e")
            validate_schema("PaymentPage", records)
            validate_schema("Payment", await store.get_payment(actor, records["items"][0]["id"]))
        else:
            records = await store.list_postings(actor, "e")
            validate_schema("PostingPage", records)
            validate_schema("Posting", await store.get_posting(actor, records["items"][0]["id"]))
    validate_schema("BusinessDocument", await store.get_payable(owner, document["id"]))


def test_reference_pack_keeps_f3_design_and_synchronizes_actual_f2_routes() -> None:
    """参考包保留规划F3，而所有已发布操作的路径方法必须同步。"""
    reference = PACKAGE.parents[1] / "docs/superpowers/specs/finance-v1/pack"
    actual = {
        name: (path, method)
        for doc in contracts().values()
        for name, (path, method, _) in operations(doc).items()
    }
    designed = {
        name: (path, method)
        for file in (reference / "apis").glob("*.yaml")
        for name, (path, method, _) in operations(yaml.safe_load(file.read_text())).items()
    }
    assert {name: designed[name] for name in actual} == actual
    assert set(designed) == set(actual)
    assert validate_package(reference).ok


def test_declared_query_selectors_match_actual_fastapi_route_contract() -> None:
    """防止目录遗漏必需选择器或分页参数漂移。"""
    documents = contracts()
    indexed = {
        name: (doc, path, method, item)
        for doc in documents.values()
        for name, (path, method, item) in operations(doc).items()
    }
    for router in (f0_router, f1_router, f2_router, f3_router, f4_router, f5_router, f6_router, f7_router):
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            doc, path, _, operation = indexed[route.operation_id]
            declared = []
            for parameter in [
                *doc["paths"][path].get("parameters", []),
                *operation.get("parameters", []),
            ]:
                if "$ref" in parameter:
                    parameter = doc["components"]["parameters"][
                        parameter["$ref"].rsplit("/", 1)[-1]
                    ]
                if parameter["in"] == "query":
                    declared.append((parameter["name"], parameter.get("required", False)))
            assert set(declared) == {
                (field.alias, field.field_info.is_required())
                for field in route.dependant.query_params
            }, route.operation_id


@pytest.mark.parametrize("wrong", ["account", "zero", "basis"])
def test_payable_draft_rejects_non_supplier_zero_or_non_purchase_basis(wrong: str) -> None:
    """应付请求schema保持实际领域模型的交叉引用边界。"""
    from .test_f2_domain import ref

    body = {
        "legal_entity_id": "e",
        "supplier_ref": ref("supplier", "supplier"),
        "amount": "10.00",
        "due_date": "2026-11-01",
        "business_refs": [ref("contract", "contract")],
        "reason": "采购",
        "evidence_ids": [],
    }
    validate_schema("PayableDraft", body)
    if wrong == "account":
        body["supplier_ref"]["kind"] = "supplier_account"
    elif wrong == "zero":
        body["amount"] = "0.00"
    else:
        body["business_refs"][0]["kind"] = "supplier"
    with pytest.raises(jsonschema.ValidationError):
        validate_schema("PayableDraft", body)


async def test_configured_integrations_and_unsent_deferred_records_match_contract(
    store: Any,
) -> None:
    """实际通道额外字段及身份服务暂不可用时的running明细也须符合发布契约。"""
    from .test_expense_domain import execute
    from .test_f2_domain import action_plan, approved_payable

    _, payer, accountant, approver, doc = await approved_payable(store)
    validate_schema("IntegrationList", await store.list_integrations(payer, "e"))
    for actor, action, schema in (
        (payer, "execute_payment", "PaymentPage"),
        (accountant, "submit_posting", "PostingPage"),
    ):
        plan = await action_plan(store, actor, approver, doc, action)
        await execute(store, actor, plan)
        job = await store.claim_settlement()
        await store.defer_settlement(job, "IDENTITY_UNAVAILABLE", uncertain=False)
        await store.release_settlement(job)
        records = await (
            store.list_payments(actor, "e")
            if schema == "PaymentPage"
            else store.list_postings(actor, "e")
        )
        validate_schema(schema, records)
        # 本测试只核验未发延后的投影，避免下一轮认领旧资金任务。
        await store.pool.execute(
            "UPDATE finance_settlement_operation SET status='cancelled' WHERE id=$1", job["id"]
        )
