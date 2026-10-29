"""维护包目录完整性与原子发布，不自研数据库备份格式。"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from xuanpu_finance.maintenance_types import MaintenanceError


def digest_file(path: Path) -> str:
    """分块计算实际文件摘要，避免将整套密文读入内存。"""
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular_files(root: Path) -> list[Path]:
    """拒绝符号链接和特殊文件，不顺着链接读取维护边界外的数据。"""
    if root.is_symlink() or not root.is_dir():
        raise MaintenanceError('OBJECT_MISSING')
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise MaintenanceError('UNSAFE_BACKUP_PATH')
        if path.is_file():
            result.append(path)
    return result


def copy_objects(source: Path, destination: Path) -> None:
    """复制实际密文，不携带外部凭据或密钥文件。"""
    destination.mkdir(mode=0o700)
    for path in regular_files(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(path, target)
        target.chmod(0o600)


def file_manifest(root: Path) -> list[dict[str, Any]]:
    """清册逐文件绑定实际路径、长度和密文摘要。"""
    return [{'path': str(p.relative_to(root)), 'size': p.stat().st_size, 'sha256': digest_file(p)} for p in regular_files(root) if p.name != 'manifest.json']


def manifest_signature(value: dict[str, Any], key: bytes) -> str:
    """用另行保全的原件密钥认证备份清册，不能改文件再改hash蒙混恢复。"""
    body = {k: v for k, v in value.items() if k != 'manifest_hmac_sha256'}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def verify_manifest(root: Path, key: bytes) -> dict[str, Any]:
    """写恢复库前严格核对清册全部文件与独立密钥指纹。"""
    try:
        manifest: dict[str, Any] = json.loads((root / 'manifest.json').read_text())
        if manifest['format_version'] != 1 or manifest['database_schema'] != 'public':
            raise ValueError('unsupported')
        if manifest['key_fingerprint'] != hashlib.sha256(key).hexdigest():
            raise MaintenanceError('KEY_MISMATCH')
        if not hmac.compare_digest(manifest['manifest_hmac_sha256'], manifest_signature(manifest, key)):
            raise ValueError('signature')
        expected = manifest['files']
        if expected != file_manifest(root):
            raise ValueError('mismatch')
        if not (root / 'database.dump').is_file() or not (root / 'objects').is_dir():
            raise ValueError('incomplete')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise MaintenanceError('BACKUP_CORRUPT') from error
    return manifest
