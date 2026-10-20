"""长对话分段引用与消费游标的独立回归。"""
from __future__ import annotations

import pytest
from xuanpu_server.proactive.chunks import cursor_progress, parse_ref, slice_text


def test_long_chinese_message_eventually_exposes_its_tail() -> None:
    """逐段传递中文正文，末尾承诺不会因永久截断而丢失。"""
    text = "前文内容。" * 1100 + "明天下午提交最终报价。"
    chunks = []
    progress: dict[str, int] = {}
    while progress.get("message:42", 0) < len(text):
        entry = slice_text("message:42", text, progress.get("message:42", 0))
        chunks.append(entry)
        before = dict(progress)
        progress = cursor_progress([entry], progress)
        assert progress["message:42"] == entry["end"]
        assert progress != before
        assert entry["text_truncated"] is (entry["end"] < len(text))
    assert "".join(entry["text"] for entry in chunks) == text
    assert "提交最终报价" in chunks[-1]["text"]
    assert chunks[0]["ref"] == "message:42"
    assert chunks[1]["ref"] == "message:42:2000"
    assert all(entry["total_length"] == len(text) for entry in chunks)


def test_partial_progress_never_consumes_unseen_tail_or_mutates_input() -> None:
    """仅已交给模型的段落推进对应偏移，其他引用保持原进度。"""
    offsets = {"message:prior": 25, "business:session:7": 2000}
    first = slice_text("message:new", "甲" * 5000, limit=2000)
    second = slice_text("business:session:7", "乙" * 5000, offset=2000, limit=500)
    result = cursor_progress([first, second, {"ref": "item:todo:1", "data": {}}], offsets)
    assert result == {"message:prior": 25, "message:new": 2000, "business:session:7": 2500}
    assert offsets == {"message:prior": 25, "business:session:7": 2000}
    assert cursor_progress([], offsets) == offsets
    assert cursor_progress([first], {"message:new": 4000})["message:new"] == 4000


@pytest.mark.parametrize("ref", ["message:42", "business:session-uuid:103"])
@pytest.mark.parametrize("offset", [0, 1, 2000])
def test_references_round_trip_to_the_original_record(ref: str, offset: int) -> None:
    """引用唯一确定原记录与正文偏移，业务审计序号不误当成段偏移。"""
    entry = slice_text(ref, "测试" * 2000, offset=offset, limit=25)
    assert parse_ref(entry["ref"]) == (ref, offset)
    assert entry["base_ref"] == ref
    assert entry["offset"] == offset
    assert entry["end"] == offset + 25
    assert entry["text"] == ("测试" * 2000)[offset:offset + 25]


def test_empty_and_final_chunks_have_complete_progress() -> None:
    """空正文和末段明确表示已读完，不制造无进展的下一段。"""
    assert slice_text("message:empty", "") == {
        "text": "", "ref": "message:empty", "offset": 0, "end": 0,
        "text_truncated": False, "total_length": 0, "base_ref": "message:empty",
    }
    final = slice_text("message:tail", "最后一段", offset=2, limit=100)
    assert final["text"] == "一段"
    assert final["end"] == final["total_length"] == 4
    assert final["text_truncated"] is False


@pytest.mark.parametrize("ref", ["", "message:", "message:id:nope", "message:id:-1", "business:session", "business::1", "business:s:1:2:3"])
def test_invalid_chunk_references_are_rejected(ref: str) -> None:
    """不把格式错误的证据引用静默指向另一条记录。"""
    with pytest.raises(ValueError):
        parse_ref(ref)


@pytest.mark.parametrize(("offset", "limit"), [(-1, 2), (5, 2), (0, 0), (0, -1)])
def test_invalid_slice_bounds_are_rejected(offset: int, limit: int) -> None:
    """不合法的分段边界不能产生倒退或无进展的消费游标。"""
    with pytest.raises(ValueError):
        slice_text("message:id", "正文", offset=offset, limit=limit)
