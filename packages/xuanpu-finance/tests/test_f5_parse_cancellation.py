"""归档解析有明确执行期限，任务取消时必须实际终止解析进程。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import os
from pathlib import Path
from typing import Any

import pytest
from xuanpu_finance.compliance import archive_checks
from xuanpu_finance.compliance.archive_models import ArchiveArtifact
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.objects import EncryptedLocalObjects


def probe_artifact(path: Path) -> ArchiveArtifact:
    """探针正文通过实际物化摘要校验，替换的只是受隔离执行的格式检测器。"""
    raw=str(path).encode()
    return ArchiveArtifact(id='probe',path='probe.bin',kind='funding_material',sha256=hashlib.sha256(raw).hexdigest(),size=len(raw),media_type='application/pdf',source={'content_base64':base64.b64encode(raw).decode()})


async def test_cancel_materialization_terminates_actual_parser(tmp_path: Path,monkeypatch: Any) -> None:
    """取消等待后PID必须退出，不能遗留持续运行且占用资源的解析线程。"""
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    monkeypatch.setattr(archive_checks,'check_parse',importlib.import_module('f5_parse_probe').blocking_parse)
    path=tmp_path/'parser-pid';objects=EncryptedLocalObjects(tmp_path/'objects',b'k'*32)
    task=asyncio.create_task(archive_checks.materialize(probe_artifact(path),objects,None))
    try:
        async with asyncio.timeout(5):
            while not path.exists():
                await asyncio.sleep(.02)
        pid=int(path.read_text())
        assert pid!=os.getpid(),'解析不得在财务服务主进程运行'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid,0)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_parse_deadline_is_fail_closed(tmp_path: Path,monkeypatch: Any) -> None:
    """长解析触及期限后拒绝封存并终止进程，不以租约健康当解析成功。"""
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    monkeypatch.setattr(archive_checks,'check_parse',importlib.import_module('f5_parse_probe').blocking_parse)
    monkeypatch.setattr(archive_checks,'PARSE_TIMEOUT_SECONDS',.2,raising=False)
    with pytest.raises(FinanceError,match='ARCHIVE_PARSE_TIMEOUT'):
        await archive_checks.materialize(probe_artifact(tmp_path/'pid'),EncryptedLocalObjects(tmp_path/'objects',b'k'*32),None)
