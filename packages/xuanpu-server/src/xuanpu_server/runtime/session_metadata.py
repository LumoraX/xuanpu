"""会话上业务方附加的关联标识（``metadata``）的校验。"""

from __future__ import annotations

from xuanpu_server.errors import InvalidRequestError

MAX_METADATA_ITEMS = 16
MAX_METADATA_CHARS = 256


def validate_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    """业务方附加的关联标识：条数与长度有限，键值都是字符串。"""
    if not metadata:
        return {}
    if len(metadata) > MAX_METADATA_ITEMS:
        raise InvalidRequestError(f"metadata 最多 {MAX_METADATA_ITEMS} 项")
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise InvalidRequestError("metadata 的键和值必须都是字符串")
        if not key or len(key) > MAX_METADATA_CHARS or len(value) > MAX_METADATA_CHARS:
            raise InvalidRequestError(f"metadata 的键值长度须在 1 到 {MAX_METADATA_CHARS} 之间")
    return dict(metadata)


__all__ = ["MAX_METADATA_CHARS", "MAX_METADATA_ITEMS", "validate_metadata"]
