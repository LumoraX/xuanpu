"""按值脱敏：把平台要记下来的文本里出现的密钥明文换掉。

用在平台自己写出去的文本上：日志里的排障线索（异常文本、连接器的 stderr）、审计的
``detail``、交给模型的错误文本。做法是拿「这次注入出去的密钥明文」逐个替换，所以只能
挡住原样（或按常见转义写出）的明文；连接器把凭据编码、拆开后再打印，这里认不出来。

边角的取舍：

- 一个密钥是另一个的子串时，先换长的。先换短的会把长的拆成碎片，留下长密钥的其余部分。
- 过短的密钥（不足 ``MIN_REDACTABLE_LENGTH`` 个字符）不做替换：一两个字符的串在普通文本里
  到处都是，换掉会把排障线索弄得面目全非。但也不能放过它——只要文本里出现了它，整段
  文本就不记，换成一句说明。宁可丢掉这条线索，也不冒把明文写进日志的险。
- 空串不算密钥，忽略。
- 截取片段（例如 stderr 的末尾）要在脱敏之后做：先截再换，截断处可能正好把一个密钥切成
  两半，剩下的半截就认不出来了。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

MASK = "***"
"""替换密钥明文用的记号。"""

MIN_REDACTABLE_LENGTH = 4
"""能按值替换的最短密钥长度；更短的密钥一旦出现，整段文本不记。"""

WITHHELD = "[已隐去：内容里出现了过短、无法按值替换的密钥明文]"
"""文本里出现过短的密钥时，整段换成这句。"""


def _spellings(secret: str) -> set[str]:
    """密钥在文本里常见的几种写法：原样、JSON 转义（含与不含非 ASCII 转义）、Python repr。"""
    return {
        secret,
        json.dumps(secret, ensure_ascii=False)[1:-1],
        json.dumps(secret)[1:-1],
        repr(secret)[1:-1],
    }


def redact(text: str, secrets: Iterable[str]) -> str:
    """把 ``text`` 里出现的每个密钥明文换成 ``MASK``。

    Args:
        text: 要写出去的文本。
        secrets: 这次注入出去的密钥明文。

    Returns:
        脱敏后的文本；若出现了过短的密钥，返回 ``WITHHELD``。
    """
    values = {value for value in secrets if value}
    long_values = {value for value in values if len(value) >= MIN_REDACTABLE_LENGTH}
    spellings = {spelling for value in long_values for spelling in _spellings(value)}
    # 长的先换：一个密钥是另一个的子串时，先换短的会留下长密钥的残片
    for spelling in sorted(spellings, key=len, reverse=True):
        text = text.replace(spelling, MASK)
    # 长密钥换掉之后再找短的：只出现在长密钥里面的短密钥已经随长密钥一起换掉了
    if any(value in text for value in values - long_values):
        return WITHHELD
    return text


def tail(text: str, limit: int) -> str:
    """文本的末尾至多 ``limit`` 个字符；截掉了前面的话加一句说明。须在脱敏之后调用。"""
    if len(text) <= limit:
        return text
    return f"…[前面 {len(text) - limit} 字符略]" + text[-limit:]


def head(text: str, limit: int) -> str:
    """文本的开头至多 ``limit`` 个字符；截掉了后面的话加一句说明。须在脱敏之后调用。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[后面 {len(text) - limit} 字符略]"


__all__ = ["MASK", "MIN_REDACTABLE_LENGTH", "WITHHELD", "head", "redact", "tail"]
