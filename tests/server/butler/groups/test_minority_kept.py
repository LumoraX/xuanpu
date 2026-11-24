"""中分歧：管家综合并列出不一致之处；少数成员的原结论保留在交付里，不被综合抹掉。"""

from __future__ import annotations

from typing import Any

from ..conftest import enable
from .helpers import WAKE, group, member, plan, submit, summaries, until


async def test_minority_conclusion_is_kept(butler: dict[str, Any], step: Any) -> None:
    """三位成员两同一异：综合依据成员1、2，成员3 的原结论与不一致之处都在汇总里。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    replies = {"INSTR-N1": "N1：周五上线。", "INSTR-N2": "N2：周五上线没问题。",
               "INSTR-N3": "N3：应推迟到下周，测试不足。"}
    for marker, text in replies.items():
        world.models.script_for(marker, step(text=text))
    world.models.script_for(WAKE, *[step(text="可以。") for _ in replies])
    world.models.script_for("consult_compare", step(calls=[("r", "rate_divergence", {
        "level": "medium", "synthesis": "倾向周五上线", "basis": [1, 2],
        "differences": ["成员3 认为测试不足、应推迟"]})]), step(text="已比对。"))
    receipt = await submit(butler, step, "mk1", plan(
        {"mode": "consult", "members": [member(m, "什么时候上线？") for m in replies]},
        title="上线评估"))
    [group_id] = receipt["groups"]

    async def done() -> bool:
        return bool((await group(butler, group_id)).summarized)

    await until(butler, done)
    verdict = (await group(butler, group_id)).verdict(0)
    assert verdict is not None and verdict.level == "medium"
    [message] = await summaries(butler)
    text = message["text"]
    assert "管家综合（依据：成员1、成员2" in text
    assert "不一致：成员3 认为测试不足、应推迟" in text
    assert "N3：应推迟到下周，测试不足。" in text
    assert text.index("各位匠人的原结论") < text.index("N3：应推迟到下周")
