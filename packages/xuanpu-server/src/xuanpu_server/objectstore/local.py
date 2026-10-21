"""本机目录形态的对象存储：只用于试用与单机，多实例不要用。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from typing import TYPE_CHECKING

from xuanpu_server.objectstore.base import (
    TEMP_PREFIX,
    ObjectNotFoundError,
    ObjectTooLargeError,
    validate_key,
)

if TYPE_CHECKING:
    from pathlib import Path


class LocalObjectStore:
    """把对象存成 ``root`` 下的文件。写入先写同目录临时文件再原子替换。

    限制：键 ``c`` 与 ``c/d`` 不能并存（S3 可以，因为文件与目录互斥）；写入不 fsync，
    断电可能丢最近的写入。只用于试用与单机。
    """

    def __init__(self, root: Path) -> None:
        """记录根目录。

        Args:
            root: 存放对象的根目录；不存在时在首次写入时创建。
        """
        self._root = root

    def _path(self, key: str) -> Path:
        """键对应的文件路径。

        在解析符号链接之后确认仍在根目录内（纵深防御：根目录下可能有指向外部的链接）。
        会访问文件系统，只在线程里调用。

        Raises:
            ValueError: 键不合法，或解析后越出根目录。
        """
        path = (self._root / validate_key(key)).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"对象键越出存储根目录：{key!r}")
        return path

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """写入（覆盖）一个对象。本机实现不保存内容类型。"""
        del content_type
        validate_key(key)
        await asyncio.to_thread(self._write, key, data)

    def _write(self, key: str, data: bytes) -> None:
        """原子写入：同目录临时文件写完后 ``os.replace``，失败时清掉临时文件。"""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=TEMP_PREFIX)
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
        validate_key(key)
        try:
            return await asyncio.to_thread(self._read, key)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise ObjectNotFoundError(f"对象不存在：{key}") from exc

    def _read(self, key: str) -> bytes:
        """读文件内容。"""
        return self._path(key).read_bytes()

    async def get_limited(self, key: str, *, max_bytes: int) -> bytes:
        """有界读取实际内容，绝不按对象元数据分配无限内存。"""
        if max_bytes < 0:
            raise ValueError("读取上限不能为负")
        validate_key(key)
        try:
            return await asyncio.to_thread(self._read_limited, key, max_bytes)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise ObjectNotFoundError(f"对象不存在：{key}") from exc

    def _read_limited(self, key: str, max_bytes: int) -> bytes:
        """读取至上限加一字节并关闭底层流。"""
        body = self._path(key).open("rb")
        try:
            content = body.read(max_bytes + 1)
        finally:
            body.close()
        if len(content) > max_bytes:
            raise ObjectTooLargeError("对象实际大小超过读取上限")
        return content

    async def exists(self, key: str) -> bool:
        """对象是否存在。"""
        validate_key(key)
        return await asyncio.to_thread(self._is_file, key)

    def _is_file(self, key: str) -> bool:
        """键对应的文件是否存在。"""
        return self._path(key).is_file()

    async def delete(self, key: str) -> None:
        """删除对象；不存在不算错。"""
        validate_key(key)
        await asyncio.to_thread(self._unlink, key)

    def _unlink(self, key: str) -> None:
        """删文件，不存在时静默。"""
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            self._path(key).unlink()


__all__ = ["LocalObjectStore"]
