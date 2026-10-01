"""本机目录形态的对象存储：只用于试用与单机，多实例不要用。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from typing import TYPE_CHECKING

from xuanpu_server.objectstore.base import ObjectNotFoundError, validate_key

if TYPE_CHECKING:
    from pathlib import Path


class LocalObjectStore:
    """把对象存成 ``root`` 下的文件。写入先写同目录临时文件再原子替换。"""

    def __init__(self, root: Path) -> None:
        """
        Args:
            root: 存放对象的根目录；不存在时在首次写入时创建。
        """
        self._root = root

    def _path(self, key: str) -> Path:
        """键对应的文件路径（键已校验，不会越出根目录）。"""
        return self._root / validate_key(key)

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """写入（覆盖）一个对象。本机实现不保存内容类型。"""
        del content_type
        await asyncio.to_thread(self._write, self._path(key), data)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        """原子写入：同目录临时文件写完后 ``os.replace``，失败时清掉临时文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)
            raise

    async def get(self, key: str) -> bytes:
        """读出整个对象。不存在抛 ``ObjectNotFoundError``。"""
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise ObjectNotFoundError(f"对象不存在：{key}") from exc

    async def exists(self, key: str) -> bool:
        """对象是否存在。"""
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> None:
        """删除对象；不存在不算错。"""
        path = self._path(key)
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(path.unlink)


__all__ = ["LocalObjectStore"]
