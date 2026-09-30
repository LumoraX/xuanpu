"""审计内容的整理：脱敏、截断、去掉不可序列化的对象。"""

from __future__ import annotations

from xuanpu_server.audit import digest_arguments, sanitize


def test_credential_looking_strings_are_redacted() -> None:
    """键名是凭据词的字符串值脱敏，嵌套也一样。"""
    cleaned = sanitize(
        {
            "api_key": "sk-1",
            "apiKey": "sk-2",
            "access_token": "t",
            "Authorization": "Bearer x",
            "nested": {"db-password": "p", "credentials": "c"},
            "list": [{"secret": "s"}],
        }
    )
    assert cleaned == {
        "api_key": "[已脱敏]",
        "apiKey": "[已脱敏]",
        "access_token": "[已脱敏]",
        "Authorization": "[已脱敏]",
        "nested": {"db-password": "[已脱敏]", "credentials": "[已脱敏]"},
        "list": [{"secret": "[已脱敏]"}],
    }


def test_token_counts_and_plain_fields_survive() -> None:
    """用量数字、普通字段原样保留：``input_tokens`` 不是凭据。"""
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "tokenizer": "cl100k"}
    assert sanitize({"usage": usage, "text": "正文", "n": 1.5, "ok": True, "none": None}) == {
        "usage": usage,
        "text": "正文",
        "n": 1.5,
        "ok": True,
        "none": None,
    }


def test_long_strings_truncated_and_objects_stringified() -> None:
    """超长字符串截断；不可序列化的对象转成文本；嵌套过深截断。"""
    cleaned = sanitize({"long": "x" * 9000, "obj": object(), "tuple": (1, 2)})
    assert cleaned["long"].startswith("x" * 8000)
    assert "已截断" in cleaned["long"]
    assert cleaned["obj"].startswith("<object")
    assert cleaned["tuple"] == [1, 2]
    deep: dict[str, object] = {}
    node = deep
    for _ in range(12):
        node["d"] = {}
        node = node["d"]  # type: ignore[assignment]
    assert "已截断" in str(sanitize(deep))


def test_argument_digest_is_stable() -> None:
    """参数摘要与键的顺序无关。"""
    assert digest_arguments({"a": 1, "b": [1, 2]}) == digest_arguments({"b": [1, 2], "a": 1})
    assert digest_arguments({"a": 1}) != digest_arguments({"a": 2})
