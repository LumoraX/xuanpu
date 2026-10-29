"""隔离解析进程的可观察探针，仅在取消和超时回归中使用。"""
from __future__ import annotations

import os
import time
from pathlib import Path


def blocking_parse(raw: bytes,media: str,kind: str | None=None) -> None:
    """写入当前执行PID后等待，测试检查取消是否真的停止执行进程。"""
    Path(raw.decode()).write_text(str(os.getpid()))
    time.sleep(30)
