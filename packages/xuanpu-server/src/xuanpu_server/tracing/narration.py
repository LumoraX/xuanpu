"""执行旁白：匠人调用工具前对员工说的话，清洗成一句挂到工具 span 上（R7，ADR 0022）。

只处理 ``assistant_text``（模型本来就对员工说出口的话）。推理、工具参数与输出不经过这里。
"""

from __future__ import annotations

import re

SAY_MAX = 160
BUFFER_MAX = 2000
_THINK = re.compile(r"<(think|thinking)>.*?</\1>", re.IGNORECASE | re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACE = re.compile(r"\s+")
# 句末标点：取最后一句时，从倒数第二个句末之后开始
_ENDS = "。！？!?；;"


def narration(text: str) -> str | None:
    """把一段旁白清洗成一句：去推理块与控制字符、折叠空白，取最后一句，过长从开头截。

    没有可显示内容时返回 None。
    """
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", _THINK.sub("", text))).strip()
    if not cleaned:
        return None
    sentence = _last_sentence(cleaned)
    if len(sentence) > SAY_MAX:
        sentence = "…" + sentence[-(SAY_MAX - 1):]
    return sentence


def _last_sentence(text: str) -> str:
    """最后一句完整的话；整段只有一句（或末尾没有句末标点）时取最后一个句末之后的部分。"""
    body = text.rstrip(_ENDS + " ")
    cut = max(body.rfind(mark) for mark in _ENDS)
    tail = text[cut + 1:].strip() if cut >= 0 else text
    return tail or text


def append(buffer: str, delta: str) -> str:
    """缓冲追加增量；超过上限时丢掉开头，只留离动作最近的部分。"""
    joined = buffer + delta
    return joined[-BUFFER_MAX:]
