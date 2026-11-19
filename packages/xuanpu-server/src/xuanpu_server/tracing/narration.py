"""执行旁白：匠人调用工具前对员工说的话，清洗成一句挂到工具 span 上（R7，ADR 0022）。

只处理 ``assistant_text``（模型本来就对员工说出口的话）。推理、工具参数与输出不经过这里。
"""

from __future__ import annotations

import re

SAY_MAX = 160
BUFFER_MAX = 2000
_THINK = re.compile(r"<(think|thinking)>.*?</\1>", re.IGNORECASE | re.DOTALL)
# 推理块没配对时（模型没闭合就去调工具，或缓冲截掉了开头）：结束标记之前、开始标记之后都当推理丢掉
_THINK_CLOSE = re.compile(r"^.*</(?:think|thinking)>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<(?:think|thinking)>.*$", re.IGNORECASE | re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACE = re.compile(r"\s+")
_SPACE_LINES = re.compile(r"\n{3,}")
# 句末标点：放不下时按这些切句
_ENDS = "。！？!?；;"


def narration(text: str) -> str | None:
    """把一段旁白清洗成可显示的一段：去推理块与控制字符、折叠空白。

    放得下（≤ ``SAY_MAX``）就整段保留（真实模型常把「做什么」和「不做什么」拆成两句，只留
    一句会丢掉要紧的那句）；放不下时从结尾往前按整句保留，离动作最近的话优先；最后一句本身
    就过长时从开头截并加「…」。没有可显示内容时返回 None。
    """
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", strip_reasoning(text))).strip()
    if not cleaned:
        return None
    if len(cleaned) <= SAY_MAX:
        return cleaned
    kept = ""
    for sentence in reversed(_sentences(cleaned)):
        if len(sentence) + len(kept) > SAY_MAX:
            break
        kept = sentence + kept
    if kept:
        return kept.strip()
    return "…" + cleaned[-(SAY_MAX - 1):]


def strip_reasoning(text: str) -> str:
    """去掉推理：成对的块整块去掉；剩下孤立的结束标记连同之前的内容、孤立的开始标记连同之后的内容
    一并去掉。宁可少显示一句，也不让推理露出来。"""
    return _THINK_OPEN.sub("", _THINK_CLOSE.sub("", _THINK.sub("", text)))


def _sentences(text: str) -> list[str]:
    """按句末标点切开，标点留在句尾。"""
    found, start = [], 0
    for index, char in enumerate(text):
        if char in _ENDS:
            found.append(text[start:index + 1])
            start = index + 1
    if start < len(text):
        found.append(text[start:])
    return found


def append(buffer: str, delta: str) -> str:
    """缓冲追加增量；超过上限时丢掉开头，只留离动作最近的部分。"""
    joined = buffer + delta
    return joined[-BUFFER_MAX:]


def without_narration(text: str, said: list[str]) -> str:
    """从匠人一轮的回复全文里去掉已作为旁白讲过的话（每次动手前那一句），只留结果。

    旁白按原样逐段去掉；全部去掉后为空则保留原文（宁可重复，不丢结果）。
    """
    kept = text
    for line in said:
        if line and not line.startswith("…"):
            kept = kept.replace(line, "")
    kept = _SPACE_LINES.sub("\n\n", kept).strip()
    return kept or text
