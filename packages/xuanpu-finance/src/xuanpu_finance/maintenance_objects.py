"""以数据库原件责任索引与归档请求恢复原对象键并实际认证。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.maintenance_files import regular_files
from xuanpu_finance.maintenance_types import MaintenanceError
from xuanpu_finance.objects import EncryptedLocalObjects

if TYPE_CHECKING:
    from pathlib import Path

    import asyncpg


def decoded(value: Any) -> Any:
    """解码asyncpg的JSON正文，不用缺省值掩盖畸形持久请求。"""
    return json.loads(value) if isinstance(value, str) else value


async def authenticate_objects(conn: asyncpg.Connection, root: Path, key: bytes) -> dict[str, Any]:
    """必须存在的引用逐一解密并比对明文，prepared缺件及额外文件明确单列。"""
    files = await asyncio.to_thread(regular_files, root)
    for file in files:
        relative = file.relative_to(root).as_posix()
        header = await asyncio.to_thread(_cipher_header, file)
        if not re.fullmatch(r'[0-9a-f]{2}/[0-9a-f]{64}\.enc', relative) or relative[:2] != file.stem[:2] or not header:
            raise MaintenanceError('UNEXPECTED_OBJECT_FILE')
    objects = EncryptedLocalObjects(root, key)
    indexed: dict[str, tuple[str | None, int | None, bool]] = {}
    for row in await conn.fetch('SELECT object_key,actual_sha256,size_bytes,receipt_id FROM finance_item WHERE object_key IS NOT NULL'):
        indexed[row['object_key']] = (row['actual_sha256'], row['size_bytes'], row['receipt_id'] is not None)
    for row in await conn.fetch('SELECT object_key,sha256 FROM finance_receipt'):
        old = indexed.get(row['object_key'])
        indexed[row['object_key']] = (row['sha256'], old[1] if old else None, True)
    await archive_index(conn, objects, indexed)
    authenticated, prepared = [], []
    mapped = set()
    for object_key, (sha, size, required) in indexed.items():
        path = objects._path(object_key)
        mapped.add(path.relative_to(root).as_posix())
        try:
            raw = await objects.get(object_key)
        except FinanceError as error:
            if error.code == 'OBJECT_NOT_FOUND' and not required:
                prepared.append(object_key)
                continue
            code = 'OBJECT_MISSING' if error.code == 'OBJECT_NOT_FOUND' else 'OBJECT_AUTHENTICATION_FAILED'
            raise MaintenanceError(code) from error
        if (sha is not None and hashlib.sha256(raw).hexdigest() != sha) or (size is not None and len(raw) != size):
            raise MaintenanceError('OBJECT_PLAINTEXT_MISMATCH')
        authenticated.append(object_key)
    extra = sorted(p.relative_to(root).as_posix() for p in files if p.relative_to(root).as_posix() not in mapped)
    return {'authenticated_objects': len(authenticated), 'prepared_missing': prepared, 'unmapped_objects': extra, 'complete_object_validation': not extra and not prepared}


async def archive_index(conn: asyncpg.Connection, objects: EncryptedLocalObjects, indexed: dict[str, tuple[str | None, int | None, bool]]) -> None:
    """请求摘要确定物化缓存路径，描述符固定实际包摘要与全部材料。"""
    for row in await conn.fetch("SELECT status,request,request_template FROM finance_compliance_operation WHERE action='archive'"):
        request = decoded(row['request_template'])
        prefix = 'f3/archive-staging/' + request['payload_hash'] + '/'
        descriptor_key = prefix + 'descriptor.json'
        required = row['request'] is not None
        try:
            raw = await objects.get(descriptor_key)
        except FinanceError as error:
            if error.code != 'OBJECT_NOT_FOUND':
                raise MaintenanceError('OBJECT_AUTHENTICATION_FAILED') from error
            if required:
                raise MaintenanceError('OBJECT_MISSING') from error
            descriptor = None
        else:
            descriptor = decoded(raw.decode())
        indexed[descriptor_key] = (None, None, required)
        indexed[prefix+'check-batch.json'] = (None, None, descriptor is not None)
        for artifact in request['payload']['archive_snapshot']['artifacts']:
            indexed[prefix+'material-'+artifact['sha256']] = (artifact['sha256'], artifact['size'], descriptor is not None)
        if descriptor is not None:
            if descriptor['bundle_key'] != prefix+'bundle.zip':
                raise MaintenanceError('ARCHIVE_DESCRIPTOR_INVALID')
            indexed[descriptor['bundle_key']] = (descriptor['bundle_sha256'], None, True)


def _cipher_header(path: Path) -> bool:
    """额外文件也必须具有真实密文布局，文件头只用于筛选而不算认证。"""
    with path.open('rb') as stream:
        return path.stat().st_size >= 32 and stream.read(4) == b'XF01'
