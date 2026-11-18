"""极简入口真实浏览器验收与审批写入验收脚本的判定口径（脚本本身需真实模型，这里只测纯函数）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4] / "scripts/acceptance"


def _load(name: str) -> Any:
    """按路径加载验收脚本。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _step(expect: str, *cards: str, **extra: Any) -> dict[str, Any]:
    """一步的浏览器记录：一条管家回复加若干卡片。"""
    nodes = [{"kind": "assistant", "title": None, "text": "好的"}]
    nodes += [{"kind": "card", "title": title, "text": title} for title in cards]
    return {"id": "s", "expect": expect, "nodes": nodes, **extra}


def test_view_needs_a_module_card_not_just_progress() -> None:
    """查看步：只有文字或只有「进展」卡不算通过（曾因此把「打不开企业连接」误判为通过）。"""
    judge = _load("r5_entry_chat_browser").judge
    assert judge([_step("view", "你的提醒", "进展", loaded=True)], {})["s"]["ok"]
    assert not judge([_step("view", "进展", loaded=True)], {})["s"]["ok"]
    assert not judge([_step("view", loaded=True)], {})["s"]["ok"]
    assert not judge([_step("view", "你的提醒", loaded=False)], {})["s"]["ok"]


def test_confirm_needs_click_saved_and_record_in_module_api() -> None:
    """确认步：点到「确认」、看到「已保存」、原模块接口里查得到，三者缺一不可。"""
    judge = _load("r5_entry_chat_browser").judge
    done = _step("confirm", "请确认", clicked=True, saved=True)
    assert judge([done], {"s": True})["s"]["ok"]
    assert not judge([done], {"s": False})["s"]["ok"]
    assert not judge([{**done, "saved": False}], {"s": True})["s"]["ok"]
    assert not judge([{**done, "clicked": False}], {"s": True})["s"]["ok"]


def test_approval_script_reads_the_single_dispatch_row() -> None:
    """审批验收：瀑布里恰有一条派工行才取它；没有或多条都不猜。"""
    dispatch_row = _load("r5_approval_acceptance").dispatch_row
    row = {"kind": "dispatch", "state": "completed"}
    assert dispatch_row([{"kind": "butler"}, row]) == row
    assert dispatch_row([{"kind": "butler"}]) is None
    assert dispatch_row([row, {**row}]) is None
