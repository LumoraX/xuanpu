"""通过POSIX目录描述符保全独立原件密文，不跟随对象路径链接。"""
from __future__ import annotations

import errno
import os
import re
import secrets
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from xuanpu_finance.errors import FinanceError

if TYPE_CHECKING:
    from collections.abc import Iterator

_DIR_FD_SUPPORTED = {os.open, os.mkdir, os.stat, os.link, os.unlink} <= os.supports_dir_fd


def _unsafe() -> FinanceError:
    """返回不包含本机目录和对象标识的固定边界错误。"""
    return FinanceError('OBJECT_PATH_UNSAFE', 500, '原件对象路径不安全')


def _open(path: Path | str, flags: int, *, dir_fd: int | None = None) -> int:
    """仅将明确的链接或非目录错误转换为稳定领域错误。"""
    try:
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _unsafe() from exc
        raise


class LocalObjectFiles:
    """在受信服务账号独占写权限的根目录内执行纯文件IO。"""

    def __init__(self, root: Path | str) -> None:
        """注入根目录，缺少POSIX安全原语时失败关闭。"""
        self.root = Path(root)
        if os.name != 'posix' or not _DIR_FD_SUPPORTED:
            raise _unsafe()
        # 不为缺失的安全标志提供零值降级。
        self.directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.read_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        self.write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

    @contextmanager
    def _shard(self, digest: str, *, create: bool) -> Iterator[int]:
        """每次重新安全打开根和分片，所有目录描述符由上下文回收。"""
        if re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise ValueError('对象摘要无效')
        if create:
            try:
                self.root.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                # 名字已存在仍必须经NOFOLLOW重新打开，不能靠路径检查放行。
                pass
        with ExitStack() as stack:
            root_fd = _open(self.root, self.directory_flags)
            stack.callback(os.close, root_fd)
            if create:
                try:
                    os.mkdir(digest[:2], mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            shard_fd = _open(digest[:2], self.directory_flags, dir_fd=root_fd)
            stack.callback(os.close, shard_fd)
            yield shard_fd

    def _read(self, shard_fd: int, leaf: str) -> bytes:
        """先拒绝静态特殊文件，再非阻塞打开并复查实际描述符类型。"""
        metadata = os.stat(leaf, dir_fd=shard_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise _unsafe()
        with ExitStack() as stack:
            descriptor = _open(leaf, self.read_flags, dir_fd=shard_fd)
            stack.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _unsafe()
            stream = stack.enter_context(os.fdopen(descriptor, 'rb', closefd=False))
            return stream.read()

    def read(self, digest: str) -> bytes:
        """读取密文且不创建根或分片，缺件和其他IO错误保留原类型。"""
        with self._shard(digest, create=False) as shard_fd:
            return self._read(shard_fd, digest+'.enc')

    def write_once(self, digest: str, encrypted: bytes) -> bytes | None:
        """使用目录内私有临时文件与原子链接，只创建不覆盖既有对象。"""
        with self._shard(digest, create=True) as shard_fd:
            temporary = '.object-'+secrets.token_hex(16)+'.tmp'
            descriptor = os.open(temporary, self.write_flags, 0o600, dir_fd=shard_fd)
            # 只有本次成功创建后才注册删除，碰撞失败不能删除其他写入者文件。
            try:
                with ExitStack() as stack:
                    stack.callback(os.close, descriptor)
                    stream = stack.enter_context(os.fdopen(descriptor, 'wb', closefd=False))
                    stream.write(encrypted)
                try:
                    os.link(temporary, digest+'.enc', src_dir_fd=shard_fd,
                            dst_dir_fd=shard_fd, follow_symlinks=False)
                except FileExistsError:
                    return self._read(shard_fd, digest+'.enc')
                return None
            finally:
                os.unlink(temporary, dir_fd=shard_fd)
