"""长文本的稳定分段引用与实际消费进度，不把被截断尾部视为已读。"""
from __future__ import annotations

from typing import Any


def slice_text(ref: str, text: str, offset: int = 0, limit: int = 2000) -> dict[str, Any]:
    """按 Unicode 字符偏移取连续正文，保留原引用和可恢复的段落边界。"""
    if not isinstance(ref, str) or not ref:
        raise ValueError("文本引用不能为空")
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise ValueError("正文偏移须在文本范围内")
    if type(limit) is not int or limit <= 0:
        raise ValueError("分段长度须为正整数")
    end = min(offset + limit, len(text))
    return {
        "text": text[offset:end], "ref": ref if offset == 0 else f"{ref}:{offset}",
        "offset": offset, "end": end, "text_truncated": end < len(text),
        "total_length": len(text), "base_ref": ref,
    }


def cursor_progress(entries: list[dict[str, Any]], offsets: dict[str, int]) -> dict[str, int]:
    """复制已有进度，仅按实际传给模型的文本段末端推进，跳过非文本证据。"""
    progress = dict(offsets)
    for entry in entries:
        # 工作区项目等未分段证据没有这两个字段，不参与消息消费游标。
        if "base_ref" not in entry and "end" not in entry:
            continue
        ref, end = entry.get("base_ref"), entry.get("end")
        if not isinstance(ref, str) or not ref or type(end) is not int or end < 0:
            raise ValueError("文本段进度缺少有效原引用或结束偏移")
        progress[ref] = max(progress.get(ref, 0), end)
    return progress


def parse_ref(ref: str) -> tuple[str, int]:
    """还原主对话或业务审计引用，业务审计序号始终属于原引用。"""
    parts = ref.split(":")
    base_parts = {"message": 2, "business": 3}.get(parts[0])
    if base_parts is None or len(parts) not in (base_parts, base_parts + 1):
        raise ValueError("消息证据引用格式无效")
    if any(not part or part.isspace() for part in parts[1:base_parts]):
        raise ValueError("消息证据引用缺少记录标识")
    base = ":".join(parts[:base_parts])
    if len(parts) == base_parts:
        return base, 0
    suffix = parts[-1]
    if not suffix.isascii() or not suffix.isdecimal():
        raise ValueError("消息段偏移须为非负整数")
    return base, int(suffix)
