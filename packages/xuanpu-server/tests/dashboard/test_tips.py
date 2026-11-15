"""提示：确定性、最多 3 条、状态提示同位置排最前；概览响应带提示；/v1/me 告诉页面用哪个入口。"""

from __future__ import annotations

from typing import Any

from xuanpu_server.dashboard.config import MinimalEntryConfig
from xuanpu_server.dashboard.tips import build_tips

from .conftest import overview

OK = {"status": "ok", "items": []}


def test_priority_and_cap() -> None:
    """状态 → 首次使用 → 等我处理 → 今天 → 匠人 → 通用，截到 3 条。"""
    blocks = [{"key": "today", **OK, "count": 2}, {"key": "waiting", **OK, "count": 1},
              {"key": "unread", "status": "error", "count": None, "items": []}]
    tips = build_tips(blocks, artisans=["support-desk"], first_use=True)
    assert [t.kind for t in tips] == ["status", "try", "try"]
    assert tips[0].prompt is None and "更多" in tips[0].text
    assert tips[1].prompt == "明天上午9点提醒我准备周报"
    assert "1 件事" in tips[2].text


def test_same_input_same_output_and_artisans() -> None:
    """同样的输入得到同样的提示；没有待处理时轮到匠人与通用提示。"""
    blocks = [{"key": "today", **OK, "count": 0}, {"key": "waiting", **OK, "count": 0}]
    first = build_tips(blocks, artisans=["support-desk", "finance", "hr"], first_use=False)
    assert first == build_tips(blocks, artisans=["support-desk", "finance", "hr"], first_use=False)
    assert [t.text for t in first] == ["可以请「support-desk」帮忙", "可以请「finance」帮忙", "记一件待办"]
    assert build_tips([], artisans=[], first_use=False)[0].text == "记一件待办"


async def test_overview_carries_tips(board: dict[str, Any]) -> None:
    """首次使用的员工拿到「直接说出你要办的事」，不超过 3 条。"""
    body = await overview(board)
    assert 1 <= len(body["tips"]) <= 3
    assert body["tips"][0]["text"] == "直接说出你要办的事"


async def test_me_tells_which_entry(board: dict[str, Any]) -> None:
    """启用租户的员工 entry=minimal；未启用、机器身份为 classic。"""
    http, platform = board["http"], board["world"].platform
    assert (await http.get("/v1/me")).json()["entry"] == "classic"
    platform.minimal_entry = MinimalEntryConfig(enabled=True, tenants=("acme",))
    assert (await http.get("/v1/me")).json()["entry"] == "minimal"
    board["as"]["who"] = board["world"].member
    assert (await http.get("/v1/me")).json()["entry"] == "classic"
