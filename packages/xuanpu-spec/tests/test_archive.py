"""业务包归档：确定性打包，把归档当作不可信输入来解包。"""

from __future__ import annotations

import io
import stat
import zipfile
from typing import TYPE_CHECKING

import pytest
from xuanpu_spec import (
    ArchiveError,
    archive_digest,
    pack_package,
    unpack_package,
    validate_package,
)
from xuanpu_spec import archive as archive_module

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    """手工构造一个归档。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def test_pack_then_unpack_roundtrip(make_package: Callable[..., Path], tmp_path: Path) -> None:
    """打包再解包，内容一致，解出来的包仍然通过校验。"""
    source = make_package()
    (source / "skills" / "triage" / "run.sh").write_text("echo hi\n")
    (source / "skills" / "triage" / "run.sh").chmod(0o755)
    destination = tmp_path / "out"
    written = unpack_package(pack_package(source), destination)
    assert written == [
        "skills/faq/SKILL.md",
        "skills/triage/SKILL.md",
        "skills/triage/run.sh",
        "xuanpu.yaml",
    ]
    for name in written:
        assert (destination / name).read_bytes() == (source / name).read_bytes()
    assert (destination / "skills/triage/run.sh").stat().st_mode & stat.S_IXUSR
    assert not (destination / "xuanpu.yaml").stat().st_mode & stat.S_IXUSR
    assert validate_package(destination).ok


def test_packing_is_deterministic(make_package: Callable[..., Path]) -> None:
    """同样的内容总是打出逐字节相同的归档，与文件修改时间、创建顺序无关。"""
    first = make_package(name="a")
    second = make_package(name="b")
    (second / "xuanpu.yaml").touch()
    assert pack_package(first) == pack_package(second)
    assert archive_digest(pack_package(first)).startswith("sha256:")
    (second / "skills" / "faq" / "SKILL.md").write_text("changed")
    assert archive_digest(pack_package(first)) != archive_digest(pack_package(second))


def test_pack_skips_local_artifacts(make_package: Callable[..., Path], tmp_path: Path) -> None:
    """版本库、缓存、本地凭据文件不进归档。"""
    source = make_package()
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("x")
    (source / "skills" / "__pycache__").mkdir()
    (source / "skills" / "__pycache__" / "m.pyc").write_bytes(b"x")
    (source / ".env").write_text("TOKEN=secret")
    (source / ".DS_Store").write_bytes(b"x")
    written = unpack_package(pack_package(source), tmp_path / "out")
    assert all(not name.startswith((".git", ".env", ".DS_Store")) for name in written)
    assert all("__pycache__" not in name for name in written)


def test_pack_requires_manifest(tmp_path: Path) -> None:
    """目录里没有清单不能打包。"""
    with pytest.raises(ArchiveError, match=r"xuanpu\.yaml"):
        pack_package(tmp_path)


def test_pack_rejects_symlink(make_package: Callable[..., Path], tmp_path: Path) -> None:
    """包里的符号链接会被拒绝，而不是跟着链接把包外的文件打进去。"""
    source = make_package()
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret")
    (source / "skills" / "link.txt").symlink_to(secret)
    with pytest.raises(ArchiveError, match="符号链接"):
        pack_package(source)


@pytest.mark.parametrize(
    "name", ["../escape.txt", "a/../../escape.txt", "/etc/cron.d/x", "a\\..\\b.txt"]
)
def test_unpack_rejects_path_escape(tmp_path: Path, name: str) -> None:
    """路径逃逸的条目被拒绝，且什么都没写出去。"""
    data = _zip({"xuanpu.yaml": b"x", name: b"pwned"})
    with pytest.raises(ArchiveError, match="不安全的路径"):
        unpack_package(data, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "out").exists()


def test_unpack_rejects_symlink_entry(tmp_path: Path) -> None:
    """归档里的符号链接条目被拒绝。"""
    data = _zip({"xuanpu.yaml": b"x"}, symlink="skills/link")
    with pytest.raises(ArchiveError, match="符号链接"):
        unpack_package(data, tmp_path / "out")


def test_unpack_requires_manifest_at_root(tmp_path: Path) -> None:
    """清单必须在归档根下，不能藏在子目录里。"""
    with pytest.raises(ArchiveError, match="根下没有"):
        unpack_package(_zip({"pkg/xuanpu.yaml": b"x"}), tmp_path / "out")


def test_unpack_rejects_duplicate_entries(tmp_path: Path) -> None:
    """同一路径出现两次：拒绝，避免「校验的是一份、落盘的是另一份」。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xuanpu.yaml", b"first")
        with pytest.warns(UserWarning, match="Duplicate"):
            archive.writestr("xuanpu.yaml", b"second")
    with pytest.raises(ArchiveError, match="重复"):
        unpack_package(buffer.getvalue(), tmp_path / "out")


def test_unpack_rejects_garbage(tmp_path: Path) -> None:
    """不是 zip 的数据。"""
    with pytest.raises(ArchiveError, match="zip"):
        unpack_package(b"definitely not a zip", tmp_path / "out")


def test_unpack_requires_empty_destination(
    make_package: Callable[..., Path], tmp_path: Path
) -> None:
    """目标目录非空时拒绝，不覆盖已有内容。"""
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "existing.txt").write_text("keep")
    with pytest.raises(ArchiveError, match="不是空的"):
        unpack_package(pack_package(make_package()), destination)
    assert (destination / "existing.txt").read_text() == "keep"


def test_size_limits(
    make_package: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单文件、总体积、文件数、归档体积的上限，打包与解包两头都生效。"""
    source = make_package()
    packed = pack_package(source)

    monkeypatch.setattr(archive_module, "MAX_FILE_BYTES", 10)
    with pytest.raises(ArchiveError, match="单文件上限"):
        pack_package(source)
    with pytest.raises(ArchiveError, match="单文件上限"):
        unpack_package(packed, tmp_path / "a")
    monkeypatch.undo()

    monkeypatch.setattr(archive_module, "MAX_UNPACKED_BYTES", 100)
    with pytest.raises(ArchiveError, match="总体积"):
        pack_package(source)
    with pytest.raises(ArchiveError, match="总体积"):
        unpack_package(packed, tmp_path / "b")
    monkeypatch.undo()

    monkeypatch.setattr(archive_module, "MAX_FILES", 2)
    with pytest.raises(ArchiveError, match="上限"):
        pack_package(source)
    with pytest.raises(ArchiveError, match="上限"):
        unpack_package(packed, tmp_path / "c")
    monkeypatch.undo()

    monkeypatch.setattr(archive_module, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ArchiveError, match="归档体积"):
        unpack_package(packed, tmp_path / "d")


def test_unpack_detects_size_mismatch(tmp_path: Path) -> None:
    """条目声明的大小与实际解出的大小不符（体积炸弹的特征）：拒绝。"""
    data = bytearray(_zip({"xuanpu.yaml": b"x" * 1000}))
    # 把中央目录与本地文件头里记录的未压缩大小（1000）都改小
    needle = (1000).to_bytes(4, "little")
    patched = bytes(data).replace(needle, (10).to_bytes(4, "little"))
    assert patched != bytes(data)
    with pytest.raises(ArchiveError, match="损坏|不符"):
        unpack_package(patched, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_failed_unpack_leaves_nothing_behind(tmp_path: Path) -> None:
    """落盘中途失败：已经写出的文件被清掉，事先存在的空目录保留为空。"""
    good = _zip({"a.txt": b"fine", "xuanpu.yaml": b"x" * 1000})
    corrupted = good.replace(b"x" * 8, b"y" * 8, 1)
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ArchiveError, match="损坏"):
        unpack_package(corrupted, destination)
    assert list(destination.iterdir()) == []
