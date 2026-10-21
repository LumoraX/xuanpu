"""已发布 F0 包通过规范校验且内核可读取真实附属材料。"""

from __future__ import annotations

from pathlib import Path

import taifeng
from xuanpu_spec import validate_package

PACKAGE = Path(__file__).resolve().parents[3] / "examples/finance"


async def test_f0_package_and_kernel_reference_read() -> None:
    """当前稳定内核加载两层 Skill，并用真实 read_skill 读取引用文件。"""
    report = validate_package(PACKAGE)
    assert report.ok, report.errors
    registry = await taifeng.FilesystemSkillRegistry.load(PACKAGE / "skills")
    snapshot = registry.snapshot()
    assert {skill.id for skill in snapshot.skills} == {"finance-assistant", "invoice-intake", "expense-reimbursement", "finance-review"}
    tool = taifeng.make_read_skill_tool()
    context = taifeng.ToolContext(
        call_id="test",
        cancel=taifeng.CancellationToken(),
        thread_id="test",
        extras={
            "skill_snapshot": snapshot,
            "visible_skills": frozenset({"finance-assistant", "invoice-intake", "expense-reimbursement", "finance-review"}),
        },
    )
    result = await tool.handler(
        {"skill_id": "invoice-intake", "path": "references/execution-rules.md"}, context
    )
    assert not result.is_error and "not_checked" in result.output
