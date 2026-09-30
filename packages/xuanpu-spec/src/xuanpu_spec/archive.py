"""业务包的归档格式：一个 zip 文件，根下是 ``xuanpu.yaml``。

打包是确定性的——同样的目录内容总是得到逐字节相同的归档——这样归档的摘要可以当作
业务包版本的指纹。解包把归档当作不可信输入处理。
"""

from __future__ import annotations

import hashlib
import io
import shutil
import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from xuanpu_spec.errors import ArchiveError
from xuanpu_spec.manifest import MANIFEST_FILENAME

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FILES = 5000

# 打包时跳过的目录与文件：版本库、缓存、本地环境、凭据文件
_SKIPPED_DIRS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", ".venv", "node_modules", ".pytest_cache", ".mypy_cache"}
)
_SKIPPED_FILES = frozenset({".DS_Store", ".env"})
# 固定的时间戳与权限位，保证打包结果确定
_FIXED_TIME = (2020, 1, 1, 0, 0, 0)
_FILE_MODE = 0o644
_EXECUTABLE_MODE = 0o755


def _collect(root: Path) -> list[Path]:
    """列出要打包的文件，按路径排序。符号链接一律不打包。"""
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _SKIPPED_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ArchiveError(f"业务包里不允许符号链接：{relative.as_posix()}")
        if path.is_file() and path.name not in _SKIPPED_FILES:
            files.append(path)
    return files


def pack_package(root: Path) -> bytes:
    """把业务包目录打成归档。

    Raises:
        ArchiveError: 目录里没有清单、含符号链接，或体积、文件数超限。
    """
    if not (root / MANIFEST_FILENAME).is_file():
        raise ArchiveError(f"{root} 下没有 {MANIFEST_FILENAME}")
    files = _collect(root)
    if len(files) > MAX_FILES:
        raise ArchiveError(f"文件数 {len(files)} 超过上限 {MAX_FILES}")
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise ArchiveError(f"文件 {path.relative_to(root).as_posix()} 超过单文件上限")
            total += len(data)
            if total > MAX_UNPACKED_BYTES:
                raise ArchiveError("业务包解包后的总体积超过上限")
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            mode = _EXECUTABLE_MODE if executable else _FILE_MODE
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, data)
    packed = buffer.getvalue()
    if len(packed) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("归档体积超过上限")
    return packed


def archive_digest(data: bytes) -> str:
    """归档的指纹。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _checked_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """逐项检查归档成员，返回要解出的普通文件。"""
    members = archive.infolist()
    if len(members) > MAX_FILES:
        raise ArchiveError(f"归档里的条目数 {len(members)} 超过上限 {MAX_FILES}")
    total = 0
    files: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    for member in members:
        name = member.filename
        pure = PurePosixPath(name)
        if "\\" in name or pure.is_absolute() or ".." in pure.parts or name.startswith("/"):
            raise ArchiveError(f"归档里有不安全的路径：{name!r}")
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ArchiveError(f"归档里不允许符号链接：{name!r}")
        if member.is_dir():
            continue
        if name in seen:
            raise ArchiveError(f"归档里有重复的路径：{name!r}")
        seen.add(name)
        if member.file_size > MAX_FILE_BYTES:
            raise ArchiveError(f"文件 {name!r} 超过单文件上限")
        total += member.file_size
        if total > MAX_UNPACKED_BYTES:
            raise ArchiveError("归档解包后的总体积超过上限")
        files.append(member)
    if MANIFEST_FILENAME not in seen:
        raise ArchiveError(f"归档根下没有 {MANIFEST_FILENAME}")
    return files


def _write_members(
    archive: zipfile.ZipFile, members: list[zipfile.ZipInfo], root: Path
) -> list[str]:
    """把检查过的成员写到目标目录。"""
    written: list[str] = []
    for member in members:
        target = (root / member.filename).resolve()
        if root not in target.parents:
            raise ArchiveError(f"归档里有不安全的路径：{member.filename!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 多读一个字节：实际内容比声明的大就是体积炸弹
            with archive.open(member) as source:
                content = source.read(member.file_size + 1)
        except (zipfile.BadZipFile, zlib.error, OSError) as exc:
            raise ArchiveError(f"文件 {member.filename!r} 损坏：{exc}") from exc
        if len(content) != member.file_size:
            raise ArchiveError(f"文件 {member.filename!r} 的实际大小与声明不符")
        target.write_bytes(content)
        executable = bool((member.external_attr >> 16) & stat.S_IXUSR)
        target.chmod(_EXECUTABLE_MODE if executable else _FILE_MODE)
        written.append(member.filename)
    return sorted(written)


def unpack_package(data: bytes, destination: Path) -> list[str]:
    """把归档解到一个空目录，返回解出的文件（相对路径，已排序）。

    归档是不可信输入：路径逃逸、符号链接、重复条目、体积炸弹、内容损坏都会被拒绝。
    全部条目先检查、后落盘；落盘中途失败时清掉已经写出的内容，不留下半个包。

    Raises:
        ArchiveError: 归档不合法，或目标目录不是空的。
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("归档体积超过上限")
    existed = destination.exists()
    if existed and any(destination.iterdir()):
        raise ArchiveError(f"目标目录不是空的：{destination}")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"不是合法的 zip 归档：{exc}") from exc
    with archive:
        members = _checked_members(archive)
        root = destination.resolve()
        root.mkdir(parents=True, exist_ok=True)
        try:
            return _write_members(archive, members, root)
        except ArchiveError:
            shutil.rmtree(root)
            if existed:
                root.mkdir()
            raise


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_UNPACKED_BYTES",
    "archive_digest",
    "pack_package",
    "unpack_package",
]
