"""小院试用的两位样例匠人（R7）：订单助手与报销助手一直通过校验、内核认得，样例办公系统能应答。"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import taifeng
from xuanpu_spec import load_openapi, validate_package

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
EXPECTED = {
    "order-desk": ("orders", "apis/orders.yaml", {"listOrders", "getOrder", "shippingReport", "markShipped"},
                   {"markShipped"}),
    "expense-helper": ("claims", "apis/expenses.yaml", {"listExpenses", "submitClaim"}, {"submitClaim"}),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_demo_package_is_valid(name: str) -> None:
    """样例包通过校验，一条提醒都没有。"""
    report = validate_package(EXAMPLES / name)
    assert report.errors == () and report.warnings == () and report.ok


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_demo_skills_load_and_writes_are_not_read_only(name: str) -> None:
    """入口 skill 能加载，可见工具含连接器工具；写操作在文档里不是只读（会要本人批准）。"""
    entry, api, operations, writes = EXPECTED[name]
    registry = await taifeng.FilesystemSkillRegistry.load(EXAMPLES / name / "skills")
    skill = registry.snapshot().get(entry)
    assert skill is not None
    connector = {"order-desk": "orders", "expense-helper": "expenses"}[name]
    assert {f"api__{connector}__{op}" for op in operations} <= skill.visible_tool_names()
    document = load_openapi(EXAMPLES / name / api)
    assert set(document.operations) == operations
    assert {op for op in operations if not document.operations[op].read_only} == writes


def test_demo_office_api_answers() -> None:
    """样例办公系统：要凭据；能列出待发货订单、给出发货统计、新建报销单。"""
    spec = importlib.util.spec_from_file_location("demo_office", EXAMPLES / "demo-office-api" / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path: str, method: str = "GET", body: dict | None = None, token: str = "demo-token") -> dict:
        """以给定凭据调一次。"""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 —— 本机回环
            return dict(json.loads(response.read()))

    try:
        with pytest.raises(urllib.error.HTTPError):
            call("/orders", token="wrong")
        waiting = call("/orders?status=paid")["orders"]
        assert {o["id"] for o in waiting} == {"O-2001", "O-2003", "O-2004"}
        assert call("/reports/shipping")["waiting"] == 3
        created = call("/expenses", "POST", {"title": "客户午餐", "amount": 186, "has_invoice": False})
        assert created["status"] == "submitted"
    finally:
        server.shutdown()
