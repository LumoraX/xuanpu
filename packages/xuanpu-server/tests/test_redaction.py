"""按值脱敏与内核 MCP 日志的过滤器。"""

from __future__ import annotations

import logging

import pytest
from xuanpu_server.redaction import MASK, WITHHELD, redact, tail
from xuanpu_server.runtime.connector_process import (
    KernelMcpLogGuard,
    install_kernel_mcp_log_guard,
)


def test_secret_is_replaced_everywhere() -> None:
    """密钥明文出现几次换几次，其余文本不动。"""
    assert redact("a=s3cret; b=s3cret!", ["s3cret"]) == f"a={MASK}; b={MASK}!"
    assert redact("没有密钥的文本", ["s3cret"]) == "没有密钥的文本"


def test_longer_secret_wins_when_one_contains_another() -> None:
    """一个密钥是另一个的子串：先换长的，不留下长密钥的残片。"""
    text = "token=abcd-efgh-ijkl short=abcd"
    assert redact(text, ["abcd", "abcd-efgh-ijkl"]) == f"token={MASK} short={MASK}"
    assert redact(text, ["abcd-efgh-ijkl", "abcd"]) == f"token={MASK} short={MASK}"


def test_short_secret_withholds_the_whole_text() -> None:
    """不足 4 个字符的密钥不按值替换；一旦出现，整段文本不记。没出现就照常。"""
    assert redact("pin=123 其余内容", ["123"]) == WITHHELD
    assert redact("pin=*** 其余内容", ["456"]) == "pin=*** 其余内容"


def test_short_secret_inside_a_long_one_is_already_gone() -> None:
    """短密钥只出现在长密钥里面：长密钥换掉之后它也就不在了，文本照常保留。"""
    assert redact("key=xy-long-secret", ["xy", "xy-long-secret"]) == f"key={MASK}"


def test_empty_secret_is_ignored() -> None:
    """空串不算密钥。"""
    assert redact("任何文本", ["", "s3cret"]) == "任何文本"


@pytest.mark.parametrize(
    ("spelled", "expected"),
    [
        ('value: "pa\\"ss\\\\wörd"', 'value: "***"'),  # JSON，非 ASCII 原样
        ('value: "pa\\"ss\\\\w\\u00f6rd"', 'value: "***"'),  # JSON，非 ASCII 转义
        ("value: 'pa\"ss\\\\wörd'", "value: '***'"),  # Python repr
    ],
)
def test_escaped_spellings_are_replaced(spelled: str, expected: str) -> None:
    """密钥里有引号、反斜杠、非 ASCII 时，按 JSON 或 repr 转义后写出的样子也认得出来。"""
    assert redact(spelled, ['pa"ss\\wörd']) == expected


def test_tail_marks_what_was_cut() -> None:
    """只留末尾时注明截掉了多少；没超长原样返回。"""
    assert tail("abcdef", 10) == "abcdef"
    assert tail("abcdef", 2) == "…[前面 4 字符略]ef"


def test_cut_after_redacting_never_leaves_half_a_secret() -> None:
    """先脱敏再截，截断处落在密钥中间也不留半截明文；反过来先截再换就会漏出后半截。"""
    secret = "0123456789abcdef"
    text = "x" * 10 + secret + "y" * 5
    assert tail(redact(text, [secret]), 6) == "…[前面 12 字符略]*yyyyy"
    assert "abcdef" in redact(tail(text, 11), [secret])


def test_kernel_mcp_log_guard_hides_raw_output(caplog: pytest.LogCaptureFixture) -> None:
    """内核 MCP 日志：字节串（连接器的原始输出）只记长度；超长参数截断并标注；数字原样。"""
    install_kernel_mcp_log_guard()
    install_kernel_mcp_log_guard()
    target = logging.getLogger("taifeng.mcp.stdio_client")
    assert sum(isinstance(item, KernelMcpLogGuard) for item in target.filters) == 1
    caplog.set_level(logging.DEBUG, logger="taifeng.mcp")
    target.warning("mcp: invalid JSON: %r", b"debug: token=s3cret-token-value\n")
    target.warning("mcp: response for unknown or abandoned request id=%r", 42)
    logging.getLogger("taifeng.mcp.server_messages").warning("method %r", {"m": "x" * 500})
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == "mcp: invalid JSON: <连接器输出 32 字节，内容不记>"
    assert messages[1].endswith("id=42")
    assert "已截断，原长" in messages[2]
    assert len(messages[2]) < 300
    assert "s3cret" not in caplog.text
