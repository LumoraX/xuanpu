"""对象存储的协议、异常与键校验。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from xuanpu_server.errors import PlatformError


class ObjectNotFoundError(PlatformError):
    """对象不存在。"""

    code = "object_not_found"
    status = 404


@runtime_checkable
class ObjectStore(Protocol):
    """对象存储：文件内容与业务包归档放在这里（ADR 0009、ADR 0011 决策 8）。

    键是以 ``/`` 分隔的相对路径，由调用方保证以租户开头（规则 3）。实现不做租户判断。
    """

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """写入（覆盖）一个对象。"""
        ...

    async def get(self, key: str) -> bytes:
        """读出整个对象。不存在抛 ``ObjectNotFoundError``。"""
        ...

    async def exists(self, key: str) -> bool:
        """对象是否存在。"""
        ...

    async def delete(self, key: str) -> None:
        """删除对象；不存在不算错。"""
        ...


def validate_key(key: str) -> str:
    """校验键的形状并原样返回。

    键必须非空、不以 ``/`` 开头、不含反斜杠，且每一段都不为空、不是 ``.`` 或 ``..``。

    Raises:
        ValueError: 键不合法。
    """
    if not key:
        raise ValueError("对象键不能为空")
    if key.startswith("/"):
        raise ValueError(f"对象键不能以 / 开头：{key!r}")
    if "\\" in key:
        raise ValueError(f"对象键不能含反斜杠：{key!r}")
    for segment in key.split("/"):
        if not segment:
            raise ValueError(f"对象键不能含空段：{key!r}")
        if segment in {".", ".."}:
            raise ValueError(f"对象键不能含 . 或 .. 段：{key!r}")
    return key


__all__ = ["ObjectNotFoundError", "ObjectStore", "validate_key"]
