"""评测运行器。"""

from __future__ import annotations

from typing import Any

import pytest
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import InvalidRequestError, PermissionDeniedError
from xuanpu_spec import pack_package

EVALS = """\
  evals:
    - name: greet
      entry: triage
      input: 你好
      expect:
        contains: ["您好"]
        notContains: ["再见"]
        maxTokens: 500
    - name: refund
      entry: triage
      input: 退款多久到账
      expect:
        contains: ["7 个工作日"]
    - name: budget
      entry: triage
      input: 讲个长故事
      expect:
        maxTokens: 50
"""


async def publish_with_evals(world: Any) -> None:
    """发布一个带评测用例的业务包版本。"""
    world.archive()
    root = world.package_dir / "1.0.0-standard-False"
    manifest = root / "xuanpu.yaml"
    manifest.write_text(manifest.read_text() + EVALS, encoding="utf-8")
    await world.platform.packages.publish(world.admin, pack_package(root), activate=True)


async def test_report_marks_each_case(world: Any, step: Any) -> None:
    """三个用例：一个通过、一个回复不符、一个超预算；报告逐条说明，会话跑完即关闭。"""
    await publish_with_evals(world)
    world.models.script(
        step(text="您好，请问有什么可以帮您？"),
        step(text="退款在 3 个工作日内到账"),
        step(text="从前有座山……"),
    )
    report = await world.platform.evals.run(world.admin, "support-desk")
    assert (report.package, report.version, report.passed) == ("support-desk", "1.0.0", False)
    by_name = {case.name: case for case in report.cases}
    assert by_name["greet"].passed
    assert by_name["greet"].outcome == "completed"
    assert by_name["greet"].total_tokens == 120
    assert by_name["refund"].reasons == ("回复里没有 '7 个工作日'",)
    assert by_name["budget"].reasons == ("消耗 120 token，超过上限 50",)
    assert all(case.session_id for case in report.cases)

    listed = await world.platform.sessions.list(world.admin, status="active")
    assert listed == []
    audit = await world.platform.store.query_audit("acme", AuditQuery(kind="eval_completed"))
    assert audit[-1].data["failed"] == ["budget", "refund"] or set(audit[-1].data["failed"]) == {
        "budget",
        "refund",
    }
    assert report.as_dict()["failed"] == 2


async def test_selected_cases_and_unknown_names(world: Any, step: Any) -> None:
    """只跑指定的用例；用例名不存在要报错。"""
    await publish_with_evals(world)
    world.models.script(step(text="您好"))
    report = await world.platform.evals.run(world.admin, "support-desk", names=["greet"])
    assert [case.name for case in report.cases] == ["greet"]
    assert report.passed
    with pytest.raises(InvalidRequestError, match="ghost"):
        await world.platform.evals.run(world.admin, "support-desk", names=["greet", "ghost"])


async def test_suspended_turn_fails_the_case(world: Any, step: Any) -> None:
    """用例里的对话停在审批点：算没通过，原因说清楚。"""
    await publish_with_evals(world)
    world.models.script(step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]))
    report = await world.platform.evals.run(world.admin, "support-desk", names=["greet"])
    case = report.cases[0]
    assert case.outcome == "suspended"
    assert not case.passed
    assert "suspended" in case.reasons[0]


async def test_package_without_evals_and_permissions(world: Any) -> None:
    """没有评测用例的包不能跑评测；成员没有权限。"""
    await world.publish()
    with pytest.raises(InvalidRequestError, match="没有评测用例"):
        await world.platform.evals.run(world.admin, "support-desk")
    with pytest.raises(PermissionDeniedError):
        await world.platform.evals.run(world.member, "support-desk")


async def test_platform_errors_become_case_failures(world: Any, step: Any) -> None:
    """密钥缺失这类平台错误不让整个评测炸掉，而是记在对应用例上。"""
    await publish_with_evals(world)
    await world.platform.delete_secret(world.admin, "crm-token")
    report = await world.platform.evals.run(world.admin, "support-desk", names=["greet"])
    case = report.cases[0]
    assert (case.passed, case.outcome) == (False, "error")
    assert case.reasons[0].startswith("secret_missing")


async def test_evals_over_http_and_sdk(world: Any, step: Any) -> None:
    """经 HTTP 接口与 SDK 运行评测。"""
    import httpx
    from xuanpu_sdk import XuanpuClient
    from xuanpu_server.api import create_app

    await publish_with_evals(world)
    world.models.script(step(text="您好"))
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with XuanpuClient("http://xuanpu", world.admin_key, transport=transport) as client:
        report = await client.packages.evaluate("support-desk", names=["greet"])
    assert report["passed"] is True
    assert report["cases"][0]["name"] == "greet"
