"""F13真实路径、外部快照与可回收阻塞探针辅助。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

KEY = 'tenant/company/original'
CONTENT = b'f13 opaque original'


def snapshot(root: Path) -> dict[str, str]:
    """只记录私有外部目录的相对文件名及密文摘要。"""
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file()}


def fifo_probe(root: Path, action: str) -> dict[str, object]:
    """在自有独立进程限时执行FIFO访问，超时强制回收而不挂线程池。"""
    program = '''
import asyncio, json, sys
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.objects import EncryptedLocalObjects
async def main():
    objects = EncryptedLocalObjects(sys.argv[1], b'k'*32)
    try:
        if sys.argv[2] == 'get':
            await objects.get('tenant/company/original')
        else:
            await objects.put('tenant/company/original', b'f13 opaque original')
    except FinanceError as exc:
        print(json.dumps({'code': exc.code, 'status': exc.status}))
    except Exception as exc:
        print(json.dumps({'code': type(exc).__name__}))
    else:
        print(json.dumps({'code': 'NO_ERROR'}))
asyncio.run(main())
'''
    process = subprocess.Popen([sys.executable, '-c', program, str(root), action],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, error = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=3)
        return {'code': 'BLOCKED', 'reaped': process.poll() is not None}
    assert process.returncode == 0, error.decode()
    return json.loads(output)
