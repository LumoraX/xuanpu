"""单块失败只影响该块：平台错误给错误码，意外错误记日志后给 internal_error；不调模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.core.errors import RuntimeUnavailableError

from .conftest import overview

if TYPE_CHECKING:
    import pytest


async def test_one_block_failing_leaves_others(board: dict[str, Any],
                                               monkeypatch: pytest.MonkeyPatch,
                                               caplog: pytest.LogCaptureFixture) -> None:
    """未读通知读不出来、待处理来源报平台错误：两块各自失败，今天照常。"""
    platform = board["world"].platform

    async def broken(*_: object, **__: object) -> object:
        """模拟存储故障。"""
        raise OSError("磁盘坏了")

    async def unavailable(*_: object, **__: object) -> object:
        """模拟平台错误。"""
        raise RuntimeUnavailableError("暂不可用")

    monkeypatch.setattr(platform.reminders.store, "unread_notifications", broken)
    monkeypatch.setattr(platform.workspace, "pending_proposals", unavailable)
    body = (await overview(board))["by_key"]
    assert body["unread"] == {"key": "unread", "status": "error", "error": "internal_error",
                              "count": None, "items": []}
    assert body["waiting"]["status"] == "error"
    assert body["waiting"]["error"] == RuntimeUnavailableError.code
    assert body["today"]["status"] == "ok"
    assert "概览块 unread 读取失败" in caplog.text


async def test_no_model_call(board: dict[str, Any]) -> None:
    """概览全程不调模型：模型脚本为空也能读出。"""
    world = board["world"]
    calls_before = len(world.models.requests)
    await overview(board)
    assert len(world.models.requests) == calls_before
